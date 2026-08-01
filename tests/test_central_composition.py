"""RB3.1a Central composition and marker-last migration contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
from typing import cast

import pytest
import yaml

from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorizationGrant,
    ResourceRef,
    SnapshotCentralAuthorizer,
    canonical_policy_digest,
    load_authority_policy_yaml,
)
from agent_org_network.central_bootstrap_admin import (
    BootstrapAdminApplication,
    BootstrapAdminAttestation,
    BootstrapAdminConfig,
    VerifiedBootstrapIdentity,
)
from agent_org_network.central_bootstrap_sqlite import CentralBootstrapAdminSealStore
from agent_org_network.central_composition import (
    CENTRAL_SCHEMA_NAME,
    CENTRAL_SCHEMA_VERSION,
    CentralCompositionUnavailable,
    CentralInstallationConfigurationError,
    compose_central,
    load_central_installation_config,
    migrate_central_schema,
    sqlite_schema_ready,
)
from agent_org_network.central_question_request_sqlite import migrate_central_question_request_schema
from agent_org_network.central_policy_revision import policy_revision_schema_ready
from agent_org_network.central_lifecycle_recovery import (
    FileReloadingLifecycleRouteAuthority,
)
import agent_org_network.central_question_lifecycle as lifecycle_module
import agent_org_network.central_composition as composition_module
from agent_org_network.central_question_lifecycle import (
    ApprovalEvaluation,
    CardBinding,
    CentralQuestionLifecycleApplication,
    CentralQuestionLifecycleStore,
    FeedbackAuthorizationProof,
    FeedbackCommand,
    OwnerAnswerCandidate,
    OwnerAnswerIngest,
    OwnerAnswerIngestApplication,
    QuestionCreateApplication,
    QuestionCreateAuthorizationProof,
    QuestionCreateCommand,
    QuestionFeedbackApplication,
    RouteAuthorization,
    SqliteCentralRegistryRootManagerResolver,
)
from agent_org_network.agent_card import AgentCard
from agent_org_network.decision import Routed, Unowned
from agent_org_network.question_request import AwaitingAnswer, QuestionRequest, RouteTarget
from agent_org_network.oidc import FakeOidcProvider
from agent_org_network.sqlite_production_registry_users import (
    ProductionRegistryUserCommand,
    ProductionRegistryUserDenied,
    SqliteProductionRegistryUsers,
)


NOW = datetime(2026, 7, 31, 5, 0, tzinfo=UTC)


def _profile(tmp_path: Path, **changes: object) -> Path:
    policy: dict[str, object] = {
        "schema_version": 1,
        "org_id": "acme",
        "policy_version": "v1",
        "content_sha256": "pending",
        "subject_roles": [
            {"org_id": "acme", "subject_id": "root", "roles": ["requester", "admin"]}
        ],
        "role_permissions": [
            {"role": "requester", "actions": ["question.create", "question.read"]},
            {"role": "admin", "actions": ["user.register"]},
        ],
        "route_rules": [],
        "worker_bindings": [],
    }
    policy["content_sha256"] = canonical_policy_digest(policy)
    authority = tmp_path / "authority.yaml"
    authority.write_text(yaml.safe_dump(policy), encoding="utf-8")
    values: dict[str, object] = {
        "profile": "local-reference",
        "org_id": "acme",
        "oidc_provider_id": "company-oidc",
        "oidc_issuer": "https://idp.example.test",
        "oidc_audience": "aon-central",
        "oidc_jwks_url": "https://idp.example.test/jwks",
        "bootstrap_oidc_device_authorization_url": "https://idp.example.test/device_authorization",
        "bootstrap_oidc_device_client_id": "aon-central-bootstrap",
        "bootstrap_oidc_scope": "openid email",
        "central_public_origin": "https://central.example.test",
        "browser_oidc_authorization_url": "https://idp.example.test/authorize",
        "browser_oidc_token_url": "https://idp.example.test/token",
        "browser_oidc_client_id": "aon-central-browser",
        "browser_oidc_scope": "openid email",
        "authority_snapshot_path": str(authority),
        "database_path": str(tmp_path / "central.sqlite3"),
        "data_directory": str(tmp_path / "data"),
        "bind_host": "127.0.0.1",
        "port": 8010,
    }
    values.update(changes)
    path = tmp_path / "central.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    return path


def _bootstrap(config_path: Path) -> None:
    config = load_central_installation_config(config_path)
    snapshot = load_authority_policy_yaml(
        config.authority_snapshot_path.read_text(encoding="utf-8"), expected_org_id="acme"
    )
    identity = VerifiedBootstrapIdentity(
        issuer=config.oidc_issuer,
        audience=config.oidc_audience,
        subject="root-subject",
        email="root@example.test",
        email_verified=True,
    )

    class Device:
        def authorize(self, **_kwargs: object) -> VerifiedBootstrapIdentity:
            return identity

    def digest(value: str) -> str:
        from hashlib import sha256

        return sha256(value.encode()).hexdigest()

    result = BootstrapAdminApplication(
        config=BootstrapAdminConfig(
            org_id=config.org_id,
            oidc_provider_id=config.oidc_provider_id,
            oidc_issuer=config.oidc_issuer,
            oidc_audience=config.oidc_audience,
            authority_policy_digest=snapshot.content_sha256,
        ),
        authority=SnapshotCentralAuthorizer(snapshot),
        device_authorizer=Device(),
        registry_path=config.database_path,
        seals=CentralBootstrapAdminSealStore(config.database_path),
        clock=lambda: NOW,
    ).run(
        BootstrapAdminAttestation(
            schema_version=1, attestation_id="bootstrap-root", org_id=config.org_id,
            registry_user_id="root", oidc_provider_id=config.oidc_provider_id,
            oidc_issuer_digest=digest(identity.issuer), oidc_audience_digest=digest(identity.audience),
            oidc_subject_digest=digest(identity.issuer + "\x00" + identity.subject),
            verified_email_digest=digest(identity.email), device_authorization_ref="device-root",
            idempotency_key="bootstrap-root", expected_registry_revision=0,
            authority_policy_digest=snapshot.content_sha256,
        )
    )
    assert result.state == "BootstrapSealed"


class _LifecycleRouteAuthority:
    def authorize_route(self, org_id: str, intent: str, agent_id: str, transaction: sqlite3.Connection | None) -> str | None:
        _ = org_id, intent, agent_id, transaction
        return "route-v1"

    def authorize_manager(self, org_id: str, manager_id: str, transaction: sqlite3.Connection | None) -> str | None:
        _ = org_id, manager_id, transaction
        return "manager-v1"


class _LifecycleQuestionCreateAuthority:
    def __init__(self, *, policy_version: str, policy_digest: str) -> None:
        session_digest = "1" * 64
        principal = AuthenticatedPrincipal(
            org_id="acme",
            subject_id="root",
            identity_provider="browser-session",
            identity_session_id=session_digest,
        )
        self._proof = QuestionCreateAuthorizationProof(
            principal=principal,
            session_grant=AuthorizationGrant(
                org_id="acme",
                subject_id="root",
                action="session.read",
                resource=ResourceRef(
                    org_id="acme",
                    kind="browser_session",
                    resource_id=session_digest,
                    owner_subject_id="root",
                ),
                roles=("requester",),
                policy_version=policy_version,
                policy_digest=policy_digest,
            ),
            create_grant=AuthorizationGrant(
                org_id="acme",
                subject_id="root",
                action="question.create",
                resource=ResourceRef(
                    org_id="acme",
                    kind="question",
                    owner_subject_id="root",
                ),
                roles=("requester",),
                policy_version=policy_version,
                policy_digest=policy_digest,
            ),
        )

    def issue_question_create_proof(
        self, command: QuestionCreateCommand, transaction: sqlite3.Connection
    ) -> QuestionCreateAuthorizationProof:
        assert (
            command.expected_org_id,
            command.expected_requester_id,
            command.identity_session_id,
            transaction.in_transaction,
        ) == ("acme", "root", "1" * 64, True)
        return self._proof

    def verify_question_create_proof(
        self,
        proof: QuestionCreateAuthorizationProof,
        command: QuestionCreateCommand,
        transaction: sqlite3.Connection,
    ) -> bool:
        _ = command
        return proof is self._proof and transaction.in_transaction


def test_file_reloading_lifecycle_authority_returns_exact_current_yaml_provenance(
    tmp_path: Path,
) -> None:
    profile = _profile(tmp_path)
    config = load_central_installation_config(profile)
    policy_path = config.authority_snapshot_path
    policy = cast(
        dict[str, object],
        yaml.safe_load(policy_path.read_text(encoding="utf-8")),
    )
    policy["route_rules"] = [
        {"org_id": "acme", "intent": "billing", "agent_card_id": "billing-card"}
    ]
    subject_roles = cast(list[dict[str, object]], policy["subject_roles"])
    root_binding = subject_roles[0]
    roles = cast(list[str], root_binding["roles"])
    roles.append("manager")
    role_permissions = cast(
        list[dict[str, object]], policy["role_permissions"]
    )
    role_permissions.append({"role": "manager", "actions": ["manager.act"]})
    policy["content_sha256"] = canonical_policy_digest(policy)
    policy_path.write_text(yaml.safe_dump(policy), encoding="utf-8")

    adapter = FileReloadingLifecycleRouteAuthority(
        authority_policy_path=policy_path,
        org_id="acme",
    )
    policy_digest = policy["content_sha256"]
    expected = RouteAuthorization(
        policy_revision_id=f"yaml:{policy_digest}",
        policy_epoch=1,
        policy_digest=policy_digest,
    )
    with sqlite3.connect(":memory:") as transaction:
        assert adapter.authorize_route(
            "acme", "billing", "billing-card", transaction
        ) == expected
        assert adapter.authorize_manager("acme", "root", transaction) == expected
        assert adapter.current_authority(transaction) == expected
        assert adapter.authorize_route(
            "acme", "billing", "other-card", transaction
        ) is None
        assert adapter.authorize_manager("acme", "other-user", transaction) is None

        policy["policy_version"] = "v2"
        policy["content_sha256"] = canonical_policy_digest(policy)
        policy_path.write_text(yaml.safe_dump(policy), encoding="utf-8")
        reloaded = adapter.current_authority(transaction)
        reloaded_digest = policy["content_sha256"]
        assert reloaded.policy_revision_id == f"yaml:{reloaded_digest}"
        assert reloaded.policy_epoch == 1
        assert reloaded.policy_digest == reloaded_digest
        assert reloaded != expected


class _LifecycleRouter:
    def __init__(self, card: AgentCard) -> None:
        self._card = card

    def route(self, question: str) -> Routed:
        assert question == "refund"
        return Routed(primary=self._card, intent="refund")


class _LifecycleCardBinding:
    def resolve_card_binding(self, org_id: str, agent_id: str, transaction: sqlite3.Connection) -> CardBinding:
        assert org_id == "acme" and agent_id == "refund" and transaction.in_transaction
        return CardBinding(agent_id="refund", owner_id="owner", revision=1)


class _LifecycleIngestAuthority:
    def authorize_answer_ingest(self, org_id: str, delivery_subject: str, owner_id: str, agent_id: str, transaction: sqlite3.Connection) -> str | None:
        assert (org_id, delivery_subject, owner_id, agent_id) == ("acme", "owner-runtime", "owner", "refund")
        assert transaction.in_transaction
        return "ingest-v1"


class _LifecycleIngestPolicy:
    def evaluate(self, org_id: str, route: RouteTarget, candidate: OwnerAnswerCandidate) -> ApprovalEvaluation:
        assert org_id == "acme" and route.agent_id == "refund" and candidate.text
        return ApprovalEvaluation(kind="no_approval", policy_digest="p" * 64)


class _LifecycleFeedbackAuthority:
    def issue_feedback_proof(self, principal: AuthenticatedPrincipal, request: QuestionRequest, record: sqlite3.Row, transaction: sqlite3.Connection) -> FeedbackAuthorizationProof:
        session = ResourceRef(org_id=request.org_id, kind="browser_session", resource_id=principal.identity_session_id, owner_subject_id=principal.subject_id)
        feedback = ResourceRef(org_id=request.org_id, kind="question_feedback", resource_id=f"{request.request_id}:{record['record_id']}", owner_subject_id=principal.subject_id)
        return FeedbackAuthorizationProof(
            principal,
            AuthorizationGrant(org_id="acme", subject_id="user", action="session.read", resource=session, roles=("requester",), policy_version="feedback-v1", policy_digest="f" * 64),
            AuthorizationGrant(org_id="acme", subject_id="user", action="feedback.create", resource=feedback, roles=("requester",), policy_version="feedback-v1", policy_digest="f" * 64),
        )

    def verify_feedback_proof(self, proof: FeedbackAuthorizationProof, request: QuestionRequest, record: sqlite3.Row, transaction: sqlite3.Connection) -> bool:
        _ = proof, request, record
        return transaction.in_transaction


def _write_feedback_evidence_for_marker_test(database: Path) -> None:
    card = AgentCard.model_validate({
        "agent_id": "refund", "owner": "owner", "team": "support", "summary": "refund",
        "domains": ["refund"], "last_reviewed_at": "2026-07-31",
    })
    store = CentralQuestionLifecycleStore(
        database, card_binding_resolver=_LifecycleCardBinding(),
    )
    try:
        application = CentralQuestionLifecycleApplication(
            store=store, router=_LifecycleRouter(card),
            route_authority=_LifecycleRouteAuthority(), request_id_factory=lambda: "request-1", clock=lambda: NOW,
            deadline=lambda _org, _state, at: at + timedelta(minutes=5), manager_item_id_factory=lambda: "manager-1",
            root_manager_resolver=SqliteCentralRegistryRootManagerResolver(), work_ticket_id_factory=lambda: "ticket-1",
        )
        created = application.create(question="refund", org_id="acme", requester_id="user", idempotency_key="create-1")
        application.process_received(created.request.request_id)
        application.process_ready_to_dispatch(created.request.request_id)
        pending = store.get("request-1")
        assert pending is not None and isinstance(pending.state, AwaitingAnswer)
        assert store.claim_delivery("request-1", "owner-runtime", NOW, timedelta(minutes=5)) is not None
        result = OwnerAnswerIngestApplication(
            store=store, approval_policy=_LifecycleIngestPolicy(), authority=_LifecycleIngestAuthority(),
            record_id_factory=lambda: "record-1", approval_item_id_factory=lambda: "approval-1", clock=lambda: NOW + timedelta(seconds=1),
            approval_deadline=lambda _org, at: at + timedelta(minutes=5),
        ).ingest(OwnerAnswerIngest(
            ticket_id="ticket-1", request_id="request-1", expected_request_revision=pending.revision, attempt=1,
            route=pending.state.route, candidate=OwnerAnswerCandidate(text="answer", sources=("published/refund",)), delivery_subject="owner-runtime",
        ))
        assert result.record_id == "record-1"
        QuestionFeedbackApplication(
            store=store, authority=_LifecycleFeedbackAuthority(), feedback_id_factory=lambda: "feedback-1", clock=lambda: NOW + timedelta(minutes=1),
        ).submit(FeedbackCommand(
            request_id="request-1", record_id="record-1", principal=AuthenticatedPrincipal(org_id="acme", subject_id="user", identity_provider="test", identity_session_id="session-1"),
            verdict="good", comment="", idempotency_key="feedback-1",
        ))
    finally:
        store.close()


def test_profile_is_exact_and_https_only(tmp_path: Path) -> None:
    valid = load_central_installation_config(_profile(tmp_path))
    assert valid.port == 8010
    opaque_client = load_central_installation_config(
        _profile(tmp_path, browser_oidc_client_id="public/client:id?opaque=value")
    )
    assert opaque_client.browser_oidc_client_id == "public/client:id?opaque=value"
    for changes in (
        {"oidc_issuer": "http://idp.example.test"},
        {"oidc_jwks_url": "https://idp.example.test/jwks?caller=chosen"},
        {"central_public_origin": "https://central.example.test/"},
        {"browser_oidc_scope": "openid email profile"},
        {"browser_oidc_client_id": "line\nbreak"},
        {"bind_host": "localhost"},
        {"port": 8011},
        {"unknown": "field"},
    ):
        with pytest.raises(CentralInstallationConfigurationError):
            load_central_installation_config(_profile(tmp_path, **changes))


def test_marker_is_written_only_after_registry_v2_and_question_schema_readback(
    tmp_path: Path,
) -> None:
    config = load_central_installation_config(_profile(tmp_path))

    def crash(point: str) -> None:
        assert point == "before-central-marker"
        raise RuntimeError("crash-before-marker")

    with pytest.raises(CentralCompositionUnavailable):
        migrate_central_schema(config, fault_injector=crash)
    assert not sqlite_schema_ready(
        config.database_path, CENTRAL_SCHEMA_NAME, CENTRAL_SCHEMA_VERSION
    )
    with sqlite3.connect(config.database_path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "production_registry_users" in tables
    assert "question_requests" in tables
    assert "browser_oidc_transactions" in tables

    migrate_central_schema(config)
    migrate_central_schema(config)
    assert sqlite_schema_ready(
        config.database_path, CENTRAL_SCHEMA_NAME, CENTRAL_SCHEMA_VERSION
    )


def test_v20_policy_component_bootstraps_epoch_one_before_marker_readback(
    tmp_path: Path,
) -> None:
    config = load_central_installation_config(_profile(tmp_path))
    migrate_central_schema(config)
    assert CENTRAL_SCHEMA_VERSION == 20
    assert policy_revision_schema_ready(config.database_path)
    with sqlite3.connect(config.database_path) as connection:
        assert connection.execute(
            "SELECT version FROM aon_installation_schema WHERE name=?",
            (CENTRAL_SCHEMA_NAME,),
        ).fetchone() == (20,)
        row = connection.execute(
            "SELECT central_active_policy_pointers.org_id,central_active_policy_pointers.epoch,"
            "central_policy_revisions.policy_version "
            "FROM central_active_policy_pointers JOIN central_policy_revisions USING (revision_id)"
        ).fetchone()
    assert row == ("acme", 1, "v1")
    composition = compose_central(config, oidc_provider=FakeOidcProvider())
    try:
        assert composition.policy_revision is not None
        assert composition.policy_revision.active("acme").epoch == 1
    finally:
        composition.close()


def test_v20_runtime_authority_uses_db_revision_when_yaml_changes(
    tmp_path: Path,
) -> None:
    config = load_central_installation_config(_profile(tmp_path))
    migrate_central_schema(config)
    config.authority_snapshot_path.write_text("not: a policy", encoding="utf-8")
    composition = compose_central(config, oidc_provider=FakeOidcProvider())
    try:
        assert composition.authority is not None
        result = composition.authority.authorize(
            AuthenticatedPrincipal(
                org_id="acme", subject_id="root", identity_provider="test",
                identity_session_id="session-1",
            ),
            "question.create",
            ResourceRef(org_id="acme", kind="question_request", resource_id="request-1"),
        )
        assert isinstance(result, AuthorizationGrant)
        assert result.policy_version == "v1"
    finally:
        composition.close()


def test_v2_marker_upgrades_to_v3_only_after_bootstrap_schema_readback(tmp_path: Path) -> None:
    config = load_central_installation_config(_profile(tmp_path))
    SqliteProductionRegistryUsers.migrate_v2(config.database_path)
    migrate_central_question_request_schema(config.database_path)
    with sqlite3.connect(config.database_path) as connection:
        connection.execute(
            "CREATE TABLE aon_installation_schema (name TEXT PRIMARY KEY, version INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO aon_installation_schema(name,version) VALUES (?,?)",
            (CENTRAL_SCHEMA_NAME, 2),
        )

    def crash(point: str) -> None:
        if point == "before-central-marker":
            raise RuntimeError

    with pytest.raises(CentralCompositionUnavailable):
        migrate_central_schema(config, fault_injector=crash)
    assert not sqlite_schema_ready(config.database_path, CENTRAL_SCHEMA_NAME, CENTRAL_SCHEMA_VERSION)
    with sqlite3.connect(config.database_path) as connection:
        assert connection.execute(
            "SELECT version FROM aon_installation_schema WHERE name=?", (CENTRAL_SCHEMA_NAME,)
        ).fetchone() == (2,)
    migrate_central_schema(config)
    assert sqlite_schema_ready(config.database_path, CENTRAL_SCHEMA_NAME, CENTRAL_SCHEMA_VERSION)


def test_v4_marker_upgrade_mounts_card_capability_before_v5_marker(tmp_path: Path) -> None:
    config = load_central_installation_config(_profile(tmp_path))
    SqliteProductionRegistryUsers.migrate_v2(config.database_path)
    migrate_central_question_request_schema(config.database_path)
    with sqlite3.connect(config.database_path) as connection:
        connection.execute(
            "CREATE TABLE aon_installation_schema (name TEXT PRIMARY KEY, version INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO aon_installation_schema(name,version) VALUES (?,?)",
            (CENTRAL_SCHEMA_NAME, 4),
        )

    def crash(point: str) -> None:
        if point == "before-central-marker":
            raise RuntimeError("marker-last")

    with pytest.raises(CentralCompositionUnavailable):
        migrate_central_schema(config, fault_injector=crash)
    with sqlite3.connect(config.database_path) as connection:
        assert connection.execute(
            "SELECT version FROM aon_installation_schema WHERE name=?", (CENTRAL_SCHEMA_NAME,)
        ).fetchone() == (4,)
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='production_agent_cards'"
        ).fetchone() == (1,)
    migrate_central_schema(config)
    assert sqlite_schema_ready(config.database_path, CENTRAL_SCHEMA_NAME, CENTRAL_SCHEMA_VERSION)


def test_valid_v6_b1_catalog_upgrades_to_v9_before_marker_write(tmp_path: Path) -> None:
    config = load_central_installation_config(_profile(tmp_path))
    migrate_central_schema(config)
    with sqlite3.connect(config.database_path) as connection:
        connection.execute("DROP TABLE central_question_feedback_audits")
        connection.execute("DROP TABLE central_question_feedback_receipts")
        connection.execute("DROP TABLE central_question_feedback_records")
        connection.execute("DROP TABLE central_question_approval_disposition_audits")
        connection.execute("DROP TABLE central_question_approval_disposition_receipts")
        connection.execute("DROP TABLE central_question_answer_ingest_audits")
        connection.execute("DROP TABLE central_question_approval_items")
        connection.execute("DROP TABLE central_question_answer_records")
        connection.execute("DROP TABLE central_question_answer_ingest_receipts")
        connection.execute("DROP TABLE central_question_work_ticket_delivery_claims")
        connection.execute("DROP TABLE central_question_work_ticket_receipts")
        connection.execute("DROP TABLE central_question_work_tickets")
        connection.execute("DROP TABLE central_question_conflict_cases")
        connection.execute(
            "UPDATE aon_installation_schema SET version=6 WHERE name=?", (CENTRAL_SCHEMA_NAME,)
        )

    assert not sqlite_schema_ready(config.database_path, CENTRAL_SCHEMA_NAME, CENTRAL_SCHEMA_VERSION)
    migrate_central_schema(config)
    assert sqlite_schema_ready(config.database_path, CENTRAL_SCHEMA_NAME, CENTRAL_SCHEMA_VERSION)
    with sqlite3.connect(config.database_path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='central_question_conflict_cases'"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='central_question_work_ticket_delivery_claims'"
        ).fetchone() == (1,)


def test_v7_delivery_catalog_upgrades_forward_to_v9_answer_ingest_catalog(tmp_path: Path) -> None:
    config = load_central_installation_config(_profile(tmp_path))
    migrate_central_schema(config)
    with sqlite3.connect(config.database_path) as connection:
        ticket_ddl = str(connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='central_question_work_tickets'"
        ).fetchone()[0]).replace("CHECK(status IN ('pending','completed'))", "CHECK(status='pending')")
        receipt_ddl = str(connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='central_question_work_ticket_receipts'"
        ).fetchone()[0])
        claim_ddl = str(connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='central_question_work_ticket_delivery_claims'"
        ).fetchone()[0])
        connection.execute("DROP TABLE central_question_feedback_audits")
        connection.execute("DROP TABLE central_question_feedback_receipts")
        connection.execute("DROP TABLE central_question_feedback_records")
        connection.execute("DROP TABLE central_question_approval_disposition_audits")
        connection.execute("DROP TABLE central_question_approval_disposition_receipts")
        connection.execute("DROP TABLE central_question_answer_ingest_audits")
        connection.execute("DROP TABLE central_question_approval_items")
        connection.execute("DROP TABLE central_question_answer_records")
        connection.execute("DROP TABLE central_question_answer_ingest_receipts")
        connection.execute("DROP TABLE central_question_work_ticket_delivery_claims")
        connection.execute("DROP TABLE central_question_work_ticket_receipts")
        connection.execute("DROP TABLE central_question_work_tickets")
        for ddl in (ticket_ddl, receipt_ddl, claim_ddl):
            connection.execute(ddl)
        connection.execute(
            "UPDATE aon_installation_schema SET version=7 WHERE name=?", (CENTRAL_SCHEMA_NAME,)
        )
    migrate_central_schema(config)
    assert sqlite_schema_ready(config.database_path, CENTRAL_SCHEMA_NAME, CENTRAL_SCHEMA_VERSION)
    with sqlite3.connect(config.database_path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='central_question_answer_ingest_receipts'"
        ).fetchone() == (1,)


def test_v13_feedback_then_v15_conflict_keeps_installation_marker_and_data_marker_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_central_installation_config(_profile(tmp_path))
    migrate_central_schema(config)
    # This fixture represents data committed before the v19 Authority-
    # provenance cutover.  The legacy direct lifecycle seam is valid only
    # while the installation marker still advertises that pre-v19 contract.
    with sqlite3.connect(config.database_path) as connection:
        connection.execute(
            "UPDATE aon_installation_schema SET version=? WHERE name=?",
            (CENTRAL_SCHEMA_VERSION - 2, CENTRAL_SCHEMA_NAME),
        )
    _write_feedback_evidence_for_marker_test(config.database_path)
    with sqlite3.connect(config.database_path) as connection:
        connection.row_factory = sqlite3.Row
        audit = connection.execute("SELECT * FROM central_question_feedback_audits").fetchone()
        assert audit is not None
        connection.execute("DROP TABLE central_question_feedback_audits")
        connection.execute(lifecycle_module._V13_TABLES[14])  # pyright: ignore[reportPrivateUsage]
        connection.execute(lifecycle_module._V13_FEEDBACK_TRIGGERS[4])  # pyright: ignore[reportPrivateUsage]
        connection.execute(lifecycle_module._V13_FEEDBACK_TRIGGERS[5])  # pyright: ignore[reportPrivateUsage]
        connection.execute(
            "INSERT INTO central_question_feedback_audits(receipt_id,feedback_id,request_id,record_id,requester_id,verdict,payload_digest,submitted_at) VALUES (?,?,?,?,?,?,?,?)",
            tuple(value for key, value in zip(audit.keys(), audit) if key != "org_id"),
        )
        connection.execute(
            "UPDATE aon_installation_schema SET version=13 WHERE name=?", (CENTRAL_SCHEMA_NAME,)
        )

    original = composition_module.migrate_central_question_lifecycle_schema

    def fail_v13_component(path: Path) -> None:
        original(
            path,
            fault_injector=lambda point: (_ for _ in ()).throw(RuntimeError("fault"))
            if point == "v13-to-current-after-schema" else None,
        )

    monkeypatch.setattr(composition_module, "migrate_central_question_lifecycle_schema", fail_v13_component)
    with pytest.raises(CentralCompositionUnavailable):
        migrate_central_schema(config)
    with sqlite3.connect(config.database_path) as connection:
        assert connection.execute(
            "SELECT version FROM aon_installation_schema WHERE name=?", (CENTRAL_SCHEMA_NAME,)
        ).fetchone() == (13,)
        assert "org_id" not in {
            row[1] for row in connection.execute("PRAGMA table_info(central_question_feedback_audits)")
        }
        assert connection.execute("SELECT COUNT(*) FROM central_question_feedback_audits").fetchone() == (1,)

    monkeypatch.setattr(composition_module, "migrate_central_question_lifecycle_schema", original)
    migrate_central_schema(config)
    with sqlite3.connect(config.database_path) as connection:
        assert connection.execute(
            "SELECT version FROM aon_installation_schema WHERE name=?", (CENTRAL_SCHEMA_NAME,)
        ).fetchone() == (CENTRAL_SCHEMA_VERSION,)
        assert connection.execute("SELECT org_id FROM central_question_feedback_audits").fetchone() == ("acme",)


def test_v8_approval_catalog_upgrades_to_v9_resolved_approval_catalog(tmp_path: Path) -> None:
    config = load_central_installation_config(_profile(tmp_path))
    migrate_central_schema(config)
    with sqlite3.connect(config.database_path) as connection:
        connection.execute("DROP TABLE central_question_feedback_audits")
        connection.execute("DROP TABLE central_question_feedback_receipts")
        connection.execute("DROP TABLE central_question_feedback_records")
        connection.execute("DROP TABLE central_question_approval_disposition_audits")
        connection.execute("DROP TABLE central_question_approval_disposition_receipts")
        connection.execute("DROP TABLE central_question_approval_items")
        connection.execute("DROP TABLE central_question_answer_records")
        connection.execute(lifecycle_module._V8_TABLES[7])  # pyright: ignore[reportPrivateUsage]
        connection.execute(lifecycle_module._V8_TABLES[8])  # pyright: ignore[reportPrivateUsage]
        connection.execute(
            "UPDATE aon_installation_schema SET version=8 WHERE name=?", (CENTRAL_SCHEMA_NAME,)
        )

    migrate_central_schema(config)

    assert sqlite_schema_ready(config.database_path, CENTRAL_SCHEMA_NAME, CENTRAL_SCHEMA_VERSION)
    with sqlite3.connect(config.database_path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='central_question_approval_disposition_receipts'"
        ).fetchone() == (1,)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(central_question_approval_items)")}
        assert {"revision", "status"} <= columns


def test_compose_does_not_seed_registry_and_readonly_registry_denies_registration(
    tmp_path: Path,
) -> None:
    profile = _profile(tmp_path)
    config = load_central_installation_config(profile)
    config.data_directory.mkdir()
    migrate_central_schema(config)
    empty = compose_central(config, oidc_provider=FakeOidcProvider())
    assert empty.intake_ready() is False
    assert empty.registry is not None
    assert empty.registry.users("acme") == ()

    _bootstrap(profile)
    capable = compose_central(
        config,
        oidc_provider=FakeOidcProvider(),
        request_id_factory=lambda: "request-1",
        clock=lambda: NOW,
    )
    assert capable.intake_ready() is True
    assert capable.registry is not None
    assert capable.approval_disposition_authority is not None
    assert capable.conflict_inbox is not None
    assert capable.conflict_concurrence is not None
    assert capable.approval_inbox is not None
    assert capable.approval_disposition is not None
    assert capable.approval_reassignment is not None
    assert capable.review_inbox is not None
    assert capable.backup_review_disposition is not None
    assert capable.reevaluation_disposition is not None
    assert capable.review_recovery is not None
    with pytest.raises(ProductionRegistryUserDenied):
        capable.registry.register(
            ProductionRegistryUserCommand(
                org_id="acme",
                principal_id="root",
                idempotency_key="forbidden-second-write",
                expected_revision=1,
                user_id="other",
                email="other@example.test",
                manager_id="root",
            )
        )


def test_compose_fails_closed_when_reopened_lifecycle_manager_binding_is_tampered(
    tmp_path: Path,
) -> None:
    profile = _profile(tmp_path)
    config = load_central_installation_config(profile)
    policy = cast(
        dict[str, object],
        yaml.safe_load(
            config.authority_snapshot_path.read_text(encoding="utf-8")
        ),
    )
    subject_roles = cast(list[dict[str, object]], policy["subject_roles"])
    cast(list[str], subject_roles[0]["roles"]).append("manager")
    role_permissions = cast(
        list[dict[str, object]], policy["role_permissions"]
    )
    role_permissions.append({"role": "manager", "actions": ["manager.act"]})
    policy["content_sha256"] = canonical_policy_digest(policy)
    config.authority_snapshot_path.write_text(
        yaml.safe_dump(policy),
        encoding="utf-8",
    )
    config.data_directory.mkdir()
    migrate_central_schema(config)
    _bootstrap(profile)

    class UnownedRouter:
        def route(self, question: str) -> Unowned:
            return Unowned(escalated_to="root", intent="refund")

    resolver = SqliteCentralRegistryRootManagerResolver()
    store = CentralQuestionLifecycleStore(
        config.database_path, root_manager_resolver=resolver
    )
    route_authority = FileReloadingLifecycleRouteAuthority(
        authority_policy_path=config.authority_snapshot_path,
        org_id=config.org_id,
    )
    application = CentralQuestionLifecycleApplication(
        store=store,
        router=UnownedRouter(),
        route_authority=route_authority,
        request_id_factory=lambda: "request-1",
        clock=lambda: NOW,
        deadline=lambda _org, _state, at: at + timedelta(minutes=5),
        manager_item_id_factory=lambda: "manager-1",
        root_manager_resolver=resolver,
    )
    snapshot = load_authority_policy_yaml(
        config.authority_snapshot_path.read_text(encoding="utf-8"),
        expected_org_id=config.org_id,
    )
    created = QuestionCreateApplication(
        store=store,
        authority=_LifecycleQuestionCreateAuthority(
            policy_version=snapshot.policy_version,
            policy_digest=snapshot.content_sha256,
        ),
        request_id_factory=lambda: "request-1",
        clock=lambda: NOW,
        deadline=lambda _org, _state, at: at + timedelta(minutes=5),
    ).create(QuestionCreateCommand(
        question="refund",
        idempotency_key="create-1",
        identity_session_id="1" * 64,
        expected_org_id="acme",
        expected_requester_id="root",
    ))
    application.process_received(created.request.request_id)
    store.close()

    with sqlite3.connect(config.database_path) as connection:
        connection.execute(
            "UPDATE central_question_manager_items SET manager_id='forged' WHERE request_id='request-1'"
        )

    composition = compose_central(config, oidc_provider=FakeOidcProvider())
    assert composition.intake_ready() is False
    assert composition.lifecycle_store is None
    composition.close()

    with sqlite3.connect(config.database_path) as connection:
        connection.execute(
            "UPDATE central_question_manager_items SET manager_id='root' WHERE request_id='request-1'"
        )
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DELETE FROM central_question_manager_items WHERE request_id='request-1'")
    missing_link = compose_central(config, oidc_provider=FakeOidcProvider())
    assert missing_link.intake_ready() is False
    assert missing_link.lifecycle_store is None
    missing_link.close()


def test_compose_fails_closed_when_conflict_reverse_catalog_is_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile(tmp_path)
    config = load_central_installation_config(profile)
    config.data_directory.mkdir()
    migrate_central_schema(config)
    _bootstrap(profile)

    def conflict_reverse_catalog_not_ready(_database_path: Path) -> bool:
        return False

    monkeypatch.setattr(
        composition_module,
        "central_inbox_conflict_schema_ready",
        conflict_reverse_catalog_not_ready,
    )

    composition = compose_central(config, oidc_provider=FakeOidcProvider())

    assert composition.intake_ready() is False
    assert composition.lifecycle_store is None
    assert composition.conflict_inbox is None
    assert composition.conflict_concurrence is None
    composition.close()


@pytest.mark.parametrize(
    "mutation",
    (
        "INSERT INTO aon_installation_schema(name,version) VALUES ('other',2)",
        "UPDATE aon_installation_schema SET version=1 WHERE name='central-installation'",
        "CREATE TRIGGER marker_trigger BEFORE INSERT ON aon_installation_schema BEGIN SELECT 1; END",
    ),
)
def test_marker_catalog_or_content_mutation_is_not_ready_and_migration_fails_closed(
    tmp_path: Path, mutation: str
) -> None:
    config = load_central_installation_config(_profile(tmp_path))
    migrate_central_schema(config)
    with sqlite3.connect(config.database_path) as connection:
        connection.executescript(mutation)
    assert not sqlite_schema_ready(
        config.database_path, CENTRAL_SCHEMA_NAME, CENTRAL_SCHEMA_VERSION
    )
    with pytest.raises(CentralCompositionUnavailable):
        migrate_central_schema(config)
