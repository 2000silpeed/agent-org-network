# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportMissingParameterType=false, reportUnknownParameterType=false
# pyright: reportArgumentType=false
# pyright: reportUnknownArgumentType=false

from datetime import UTC, datetime, timedelta
from pathlib import Path
from hashlib import sha256
import sqlite3
import yaml

from fastapi.testclient import TestClient
import pytest
from pydantic import SecretStr

from agent_org_network.central_authority import (
    SnapshotCentralAuthorizer,
    canonical_policy_digest,
    load_authority_policy_yaml,
)
from agent_org_network.central_authoring_web import create_central_authoring_app
from agent_org_network.production_authoring_authorizer import (
    ProductionCentralTxCurrentAuthoringAuthorizer,
)
from agent_org_network.production_authoring_identity import (
    AuthoringIdentitySessionRef,
    AuthoringInvocation,
    ProductionAuthoringIdentityVerifier,
)
from agent_org_network.production_identity_sessions import (
    ProductionPrincipalResolver,
    SqliteProductionIdentitySessions,
    VerifiedEmailIdentityProof,
)
from agent_org_network.sqlite_production_agent_cards import (
    CurrentCardRegistrationAuthorization,
    ProductionAgentCardCommand,
    SqliteProductionAgentCards,
)
from agent_org_network.sqlite_production_authoring_runs import (
    AuthoringRunResource,
    AuthoringSourceRef,
    CompleteAuthoringRunCommand,
    ProductionAuthoringRunDenied,
    StartAuthoringRunCommand,
    SqliteProductionAuthoringRuns,
    ProductionAuthoringRunUnavailable,
)
from agent_org_network.sqlite_production_registry_users import (
    CurrentUserRegistrationAuthorization,
    ProductionRegistryUserCommand,
    SqliteProductionRegistryUsers,
)

SESSION = "s" * 32


class _UserAuth:
    def current(self, command, transaction: sqlite3.Connection):
        return CurrentUserRegistrationAuthorization(
            authority_epoch=1, policy_digest="a" * 64, evidence_digest="b" * 64
        )

    def verify_precommit(self, command, evidence, transaction: sqlite3.Connection) -> bool:
        return True


class _CardAuth:
    def current(self, command, transaction: sqlite3.Connection):
        return CurrentCardRegistrationAuthorization(
            authority_epoch=1, policy_digest="a" * 64, evidence_digest="b" * 64
        )

    def verify_precommit(self, command, evidence, transaction: sqlite3.Connection) -> bool:
        return True


def _app(tmp_path: Path):
    database = tmp_path / "central.sqlite"
    SqliteProductionRegistryUsers.migrate(database)
    users = SqliteProductionRegistryUsers(database, authorize=_UserAuth())
    users.register(
        ProductionRegistryUserCommand(
            org_id="acme",
            principal_id="owner",
            idempotency_key="user-1",
            expected_revision=0,
            user_id="owner",
            email="owner@example.com",
        )
    )
    SqliteProductionAgentCards.migrate(database)
    cards = SqliteProductionAgentCards(database, authorize=_CardAuth())
    cards.register(
        ProductionAgentCardCommand(
            org_id="acme",
            principal_id="owner",
            idempotency_key="card-1",
            expected_revision=1,
            card={
                "agent_id": "support", "owner": "owner", "team": "support",
                "summary": "support", "domains": ["support"],
                "last_reviewed_at": "2026-07-28", "maintainer": None,
                "can_answer": [], "cannot_answer": [], "approval_when": [],
                "collaborate_when": [], "knowledge_sources": [], "trust_labels": [],
            },
        )
    )
    with sqlite3.connect(database) as connection:
        card_digest = connection.execute(
            "SELECT card_digest FROM production_agent_cards "
            "WHERE org_id='acme' AND agent_id='support'"
        ).fetchone()[0]
    SqliteProductionIdentitySessions.migrate(database)
    now = datetime(2026, 7, 28, tzinfo=UTC)
    sessions = SqliteProductionIdentitySessions(
        database,
        registry=users,
        configured_org_id="acme",
        provider_id="corp",
        issuer="https://id.example",
        clock=lambda: now,
        _identity_session_id_factory=lambda: SESSION,
    )
    sessions.establish(
        VerifiedEmailIdentityProof(
            provider_id="corp",
            issuer="https://id.example",
            email="owner@example.com",
            email_verified=True,
        ),
        expires_at=now + timedelta(hours=1),
    )
    refreshed_sessions = SqliteProductionIdentitySessions(
        database,
        registry=users,
        configured_org_id="acme",
        provider_id="corp",
        issuer="https://id.example",
        clock=lambda: now,
        _identity_session_id_factory=lambda: "t" * 32,
    )
    refreshed_sessions.establish(
        VerifiedEmailIdentityProof(
            provider_id="corp",
            issuer="https://id.example",
            email="owner@example.com",
            email_verified=True,
        ),
        expires_at=now + timedelta(hours=1),
    )
    policy: dict[str, object] = {
        "schema_version": 1,
        "org_id": "acme",
        "policy_version": "policy-v1",
        "content_sha256": "pending",
        "subject_roles": [
            {"org_id": "acme", "subject_id": "owner", "roles": ["owner"]}
        ],
        "role_permissions": [{"role": "owner", "actions": ["author.write", "author.publish"]}],
        "route_rules": [],
        "worker_bindings": [],
    }
    policy["content_sha256"] = canonical_policy_digest(policy)
    snapshot = load_authority_policy_yaml(
        yaml.safe_dump(policy, sort_keys=False), expected_org_id="acme"
    )
    authorizer = ProductionCentralTxCurrentAuthoringAuthorizer(
        policy_snapshot=lambda: snapshot,
        central_authorizer=SnapshotCentralAuthorizer(snapshot),
        identity_verifier=ProductionAuthoringIdentityVerifier(
            provider_id="corp", issuer="https://id.example", clock=lambda: now
        ),
    )
    SqliteProductionAuthoringRuns.migrate(database)
    runs = SqliteProductionAuthoringRuns(database, authorize=authorizer)
    return (
        create_central_authoring_app(
            runs=runs,
            principal_resolver=ProductionPrincipalResolver(sessions),
            authorizer=authorizer,
        ),
        card_digest,
        database,
        authorizer,
    )


def test_actual_production_uow_start는_metadata_only_exact_replay다(tmp_path: Path) -> None:
    app, card_digest, database, authorizer = _app(tmp_path)
    client = TestClient(app)
    body = {
        "agent_id": "support",
        "expected_card_revision": 2,
        "expected_card_digest": card_digest,
        "sources": [{
            "source_digest": "c" * 64,
            "byte_size": 12,
            "media_type": "text/markdown",
        }],
    }
    headers = {
        "idempotency-key": "start-1",
        "cookie": f"aon_identity_session={SESSION}",
    }
    first = client.post("/authoring/runs/start", headers=headers, json=body)
    second = client.post(
        "/authoring/runs/start",
        headers={**headers, "cookie": f"aon_identity_session={'t' * 32}"},
        json=body,
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["run"]["stage"] == "Extracting"
    assert second.json()["replayed"] is True
    complete_body = {
        "run_id": first.json()["run"]["run_id"],
        "expected_revision": 0,
        "expected_card_revision": 2,
        "expected_card_digest": card_digest,
        "admitted_bundle_digest": "e" * 64,
        "document_count": 1,
        "edge_count": 0,
        "dropped_count": 0,
        "author_profile_digest": "f" * 64,
    }
    complete_headers = {
        "idempotency-key": "complete-1",
        "cookie": f"aon_identity_session={SESSION}",
    }
    completed = client.post(
        "/authoring/runs/complete", headers=complete_headers, json=complete_body
    )
    completed_replay = client.post(
        "/authoring/runs/complete",
        headers={
            **complete_headers,
            "cookie": f"aon_identity_session={'t' * 32}",
        },
        json=complete_body,
    )
    assert completed.status_code == completed_replay.status_code == 200, (
        completed.text,
        completed_replay.text,
    )
    assert completed.json()["run"]["stage"] == "AwaitingOwnerReview"
    assert completed_replay.json()["replayed"] is True
    with sqlite3.connect(database) as connection:
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE name='production_authoring_audit_immutable'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER production_authoring_audit_immutable")
        connection.execute(
            "UPDATE production_authoring_audit_intents "
            "SET grant_evidence_digest=? WHERE event_kind='run_completed'",
            ("0" * 64,),
        )
        connection.execute(trigger_sql)
        connection.commit()
    drifted_replay = client.post(
        "/authoring/runs/complete",
        headers=complete_headers,
        json=complete_body,
    )
    resource = AuthoringRunResource(
        org_id="acme", agent_id="support", owner_id="owner",
        card_revision=2, card_digest=card_digest,
    )
    complete_command = CompleteAuthoringRunCommand(
        organization_id="acme",
        principal_id="owner",
        idempotency_key="complete-1",
        **complete_body,
    )
    invocation = AuthoringInvocation(
        session=AuthoringIdentitySessionRef(value=SecretStr(SESSION)),
        org_id="acme", principal_id="owner", identity_provider="corp",
    )
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(ProductionAuthoringRunDenied):
            authorizer.current(
                complete_command,
                resource,
                completed.json()["run"]["source_set_digest"],
                invocation,
                connection,
            )
    assert drifted_replay.status_code == 503
    with sqlite3.connect(database) as connection:
        stored = connection.execute(
            "SELECT identity_session_digest FROM production_authoring_command_receipts"
        ).fetchone()[0]
        authoring_values = tuple(
            connection.iterdump()
        )
    assert stored == sha256(SESSION.encode()).hexdigest()
    assert SESSION not in "\n".join(
        line for line in authoring_values if "production_authoring_" in line
    )
    assert "t" * 32 not in "\n".join(
        line for line in authoring_values if "production_authoring_" in line
    )
    wire = first.text
    assert "PRIVATE" not in wire
    assert "filename" not in wire
    assert "full_draft" not in wire


def test_publish_begin은_approved_review만_body없이_claim하고_exact_replay한다(tmp_path: Path) -> None:
    app, card_digest, _database, _authorizer = _app(tmp_path)
    client = TestClient(app)
    headers = {"cookie": f"aon_identity_session={SESSION}"}
    start = client.post("/authoring/runs/start", headers=headers | {"idempotency-key": "start-publish"}, json={
        "agent_id": "support", "expected_card_revision": 2, "expected_card_digest": card_digest,
        "sources": [{"source_digest": "c" * 64, "byte_size": 12, "media_type": "text/markdown"}],
    })
    assert start.status_code == 200
    run = start.json()["run"]
    complete = client.post("/authoring/runs/complete", headers=headers | {"idempotency-key": "complete-publish"}, json={
        "run_id": run["run_id"], "expected_revision": 0, "expected_card_revision": 2, "expected_card_digest": card_digest,
        "admitted_bundle_digest": "e" * 64, "document_count": 1, "edge_count": 0, "dropped_count": 0, "author_profile_digest": "f" * 64,
    })
    assert complete.status_code == 200
    review = client.post("/authoring/runs/review", headers=headers | {"idempotency-key": "review-publish"}, json={
        "run_id": run["run_id"], "expected_revision": 1, "expected_card_revision": 2, "expected_card_digest": card_digest,
        "concept_id": "bundle", "source_digest": run["source_set_digest"], "draft_digest": "e" * 64, "outcome": "Approved",
    })
    assert review.status_code == 200
    body = {"run_id": run["run_id"], "expected_revision": 2, "expected_card_revision": 2, "expected_card_digest": card_digest}
    first = client.post("/authoring/runs/publish-begin", headers=headers | {"idempotency-key": "publish-1"}, json=body)
    replay = client.post("/authoring/runs/publish-begin", headers=headers | {"idempotency-key": "publish-1"}, json=body)
    assert first.status_code == replay.status_code == 200, (first.text, replay.text)
    assert first.json()["run"]["stage"] == "Publishing"
    assert replay.json()["replayed"] is True
    assert "draft" not in first.text.lower() and "patch" not in first.text.lower()


@pytest.mark.parametrize("tamper", ["source", "anchor"])
def test_complete_current는_source_or_start_anchor_tamper를failclosed_write0(
    tmp_path: Path, tamper: str
) -> None:
    app, card_digest, database, _authorizer = _app(tmp_path)
    client = TestClient(app)
    start = client.post(
        "/authoring/runs/start",
        headers={
            "idempotency-key": "start-1",
            "cookie": f"aon_identity_session={SESSION}",
        },
        json={
            "agent_id": "support",
            "expected_card_revision": 2,
            "expected_card_digest": card_digest,
            "sources": [{
                "source_digest": "c" * 64,
                "byte_size": 12,
                "media_type": "text/markdown",
            }],
        },
    )
    assert start.status_code == 200
    run_id = str(start.json()["run"]["run_id"])
    with sqlite3.connect(database) as connection:
        if tamper == "source":
            connection.execute(
                "INSERT INTO production_authoring_source_refs VALUES (?,?,?,?,?)",
                ("acme", run_id, "d" * 64, 1, "text/plain"),
            )
        else:
            connection.execute(
                "INSERT INTO production_authoring_audit_intents "
                "(org_id,action,event_kind,principal_id,run_id,agent_id,"
                "source_set_digest,source_count,total_bytes,command_digest,"
                "policy_version,policy_digest,grant_evidence_digest,"
                "identity_session_digest,identity_evidence_digest,"
                "resource_fingerprint,result_revision,created_at) "
                "SELECT org_id,action,event_kind,principal_id,run_id,agent_id,"
                "source_set_digest,source_count,total_bytes,command_digest,"
                "policy_version,policy_digest,grant_evidence_digest,"
                "identity_session_digest,identity_evidence_digest,"
                "resource_fingerprint,result_revision,created_at "
                "FROM production_authoring_audit_intents "
                "WHERE event_kind='run_started'"
            )
        connection.commit()
    response = client.post(
        "/authoring/runs/complete",
        headers={
            "idempotency-key": "complete-1",
            "cookie": f"aon_identity_session={SESSION}",
        },
        json={
            "run_id": run_id,
            "expected_revision": 0,
            "expected_card_revision": 2,
            "expected_card_digest": card_digest,
            "admitted_bundle_digest": "e" * 64,
            "document_count": 1,
            "edge_count": 0,
            "dropped_count": 0,
            "author_profile_digest": "f" * 64,
        },
    )
    assert response.status_code == 503
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT stage,revision FROM production_authoring_runs "
            "WHERE org_id='acme' AND run_id=?",
            (run_id,),
        ).fetchone() == ("extracting", 0)
        assert connection.execute(
            "SELECT COUNT(*) FROM production_authoring_command_receipts "
            "WHERE action_kind='authoring_run.complete'"
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("table", "trigger", "assignment"),
    [
        (
            "production_authoring_audit_intents",
            "production_authoring_audit_immutable",
            "result_revision=0",
        ),
        (
            "production_authoring_outbox_intents",
            "production_authoring_outbox_immutable",
            "result_revision=0",
        ),
        (
            "production_authoring_command_receipts",
            "production_authoring_receipts_immutable",
            "created_at='malformed'",
        ),
        (
            "production_authoring_command_receipts",
            "production_authoring_receipts_immutable",
            "policy_digest='malformed'",
        ),
    ],
)
def test_completion_anchor_format_semantic_drift는standalone_current에서deny(
    tmp_path: Path, table: str, trigger: str, assignment: str
) -> None:
    app, card_digest, database, authorizer = _app(tmp_path)
    client = TestClient(app)
    started = client.post(
        "/authoring/runs/start",
        headers={
            "idempotency-key": "start-1",
            "cookie": f"aon_identity_session={SESSION}",
        },
        json={
            "agent_id": "support",
            "expected_card_revision": 2,
            "expected_card_digest": card_digest,
            "sources": [{
                "source_digest": "c" * 64,
                "byte_size": 12,
                "media_type": "text/markdown",
            }],
        },
    ).json()["run"]
    body = {
        "run_id": started["run_id"],
        "expected_revision": 0,
        "expected_card_revision": 2,
        "expected_card_digest": card_digest,
        "admitted_bundle_digest": "e" * 64,
        "document_count": 1,
        "edge_count": 0,
        "dropped_count": 0,
        "author_profile_digest": "f" * 64,
    }
    assert client.post(
        "/authoring/runs/complete",
        headers={
            "idempotency-key": "complete-1",
            "cookie": f"aon_identity_session={SESSION}",
        },
        json=body,
    ).status_code == 200
    with sqlite3.connect(database) as connection:
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name=?", (trigger,)
        ).fetchone()[0]
        connection.execute(f"DROP TRIGGER {trigger}")  # noqa: S608
        connection.execute(
            f"UPDATE {table} SET {assignment} "  # noqa: S608
            "WHERE "
            + (
                "action_kind='authoring_run.complete'"
                if table == "production_authoring_command_receipts"
                else (
                    "event_kind='run_completed'"
                    if table == "production_authoring_audit_intents"
                    else "kind='authoring.run_completed'"
                )
            )
        )
        connection.execute(trigger_sql)
        connection.commit()
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        command = CompleteAuthoringRunCommand(
            organization_id="acme",
            principal_id="owner",
            idempotency_key="complete-1",
            **body,
        )
        resource = AuthoringRunResource(
            org_id="acme", agent_id="support", owner_id="owner",
            card_revision=2, card_digest=card_digest,
        )
        invocation = AuthoringInvocation(
            session=AuthoringIdentitySessionRef(value=SecretStr(SESSION)),
            org_id="acme", principal_id="owner", identity_provider="corp",
        )
        with pytest.raises(ProductionAuthoringRunDenied):
            authorizer.current(
                command,
                resource,
                str(started["source_set_digest"]),
                invocation,
                connection,
            )


def test_start_receipt_different_valid_created_at는standalone_current에서deny(
    tmp_path: Path,
) -> None:
    app, card_digest, database, authorizer = _app(tmp_path)
    client = TestClient(app)
    response = client.post(
        "/authoring/runs/start",
        headers={
            "idempotency-key": "start-1",
            "cookie": f"aon_identity_session={SESSION}",
        },
        json={
            "agent_id": "support",
            "expected_card_revision": 2,
            "expected_card_digest": card_digest,
            "sources": [{
                "source_digest": "c" * 64,
                "byte_size": 12,
                "media_type": "text/markdown",
            }],
        },
    )
    run = response.json()["run"]
    with sqlite3.connect(database) as connection:
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE name='production_authoring_receipts_immutable'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER production_authoring_receipts_immutable")
        connection.execute(
            "UPDATE production_authoring_command_receipts "
            "SET created_at='2026-07-28T23:59:59.999Z' WHERE action_kind='start'"
        )
        connection.execute(trigger_sql)
        connection.commit()
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        command = StartAuthoringRunCommand(
            org_id="acme",
            principal_id="owner",
            idempotency_key="start-1",
            agent_id="support",
            expected_card_revision=2,
            expected_card_digest=card_digest,
            sources=(AuthoringSourceRef(
                source_digest="c" * 64,
                byte_size=12,
                media_type="text/markdown",
            ),),
        )
        resource = AuthoringRunResource(
            org_id="acme", agent_id="support", owner_id="owner",
            card_revision=2, card_digest=card_digest,
        )
        invocation = AuthoringInvocation(
            session=AuthoringIdentitySessionRef(value=SecretStr(SESSION)),
            org_id="acme", principal_id="owner", identity_provider="corp",
        )
        with pytest.raises(ProductionAuthoringRunDenied):
            authorizer.current(
                command,
                resource,
                str(run["source_set_digest"]),
                invocation,
                connection,
            )


def test_auth_idempotency_extra_raw와_review_publish는_failclosed다(
    tmp_path: Path,
) -> None:
    app, card_digest, _database, _authorizer = _app(tmp_path)
    client = TestClient(app)
    body = {
        "agent_id": "support",
        "expected_card_revision": 2,
        "expected_card_digest": card_digest,
        "sources": [{
            "source_digest": "c" * 64,
            "byte_size": 12,
            "media_type": "text/markdown",
        }],
    }
    assert client.post("/authoring/runs/start", json=body).status_code == 401
    assert client.post(
        "/authoring/runs/start",
        headers={"cookie": f"aon_identity_session={SESSION}"},
        json=body,
    ).status_code == 422
    assert client.post(
        "/authoring/runs/start",
        headers={
            "cookie": f"aon_identity_session={SESSION}",
            "idempotency-key": "start-1",
        },
        json={**body, "raw": "PRIVATE"},
    ).status_code == 422
    for forbidden in (
        {"session": SESSION},
        {"org_id": "acme"},
        {"principal_id": "owner"},
    ):
        assert client.post(
            "/authoring/runs/start",
            headers={
                "cookie": f"aon_identity_session={SESSION}",
                "idempotency-key": "start-extra",
            },
            json={**body, **forbidden},
        ).status_code == 422
    assert client.post("/authoring/review").status_code == 404
    assert client.post("/authoring/publish").status_code == 404


def test_production_UoW와_session_resolver외_injection은거부한다() -> None:
    with pytest.raises(ProductionAuthoringRunUnavailable):
        create_central_authoring_app(
            runs=object(), principal_resolver=object(), authorizer=object()
        )  # type: ignore[arg-type]
