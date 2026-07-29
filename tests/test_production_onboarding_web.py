from pathlib import Path
import sqlite3
from typing import Any, cast

from fastapi.testclient import TestClient
from httpx import Response

from agent_org_network.production_identity_sessions import (
    ProductionPrincipalResolver,
    SqliteProductionIdentitySessions,
    VerifiedEmailIdentityProof,
)
from agent_org_network.production_onboarding_web import create_production_onboarding_app
from agent_org_network.sqlite_production_agent_cards import (
    CurrentCardRegistrationAuthorization,
    SqliteProductionAgentCards,
)
from agent_org_network.sqlite_production_registry_users import (
    CurrentUserRegistrationAuthorization,
    ProductionRegistryUserCommand,
    SqliteProductionRegistryUsers,
)

_SID = "s" * 32


class _Auth:
    def current(self, command: object, transaction: sqlite3.Connection) -> CurrentUserRegistrationAuthorization:
        _ = command, transaction
        return CurrentUserRegistrationAuthorization(
            authority_epoch=1, policy_digest="b" * 64, evidence_digest="a" * 64
        )

    def verify_precommit(
        self, command: object, evidence: CurrentUserRegistrationAuthorization,
        transaction: sqlite3.Connection,
    ) -> bool:
        _ = command, evidence, transaction
        return True


class _CardAuth:
    def current(
        self, command: object, transaction: sqlite3.Connection
    ) -> CurrentCardRegistrationAuthorization:
        _ = command, transaction
        return CurrentCardRegistrationAuthorization(
            authority_epoch=1, policy_digest="d" * 64, evidence_digest="c" * 64
        )

    def verify_precommit(
        self, command: object, evidence: CurrentCardRegistrationAuthorization,
        transaction: sqlite3.Connection,
    ) -> bool:
        _ = command, evidence, transaction
        return True


def _client(
    tmp_path: Path, *, cards_enabled: bool = False, delegation: object | None = None
) -> TestClient:
    tmp_path.mkdir(parents=True, exist_ok=True)
    registry_path = tmp_path / "registry.db"
    SqliteProductionRegistryUsers.migrate(registry_path)
    users = SqliteProductionRegistryUsers(registry_path, authorize=_Auth())
    users.register(
        ProductionRegistryUserCommand(
            org_id="acme", principal_id="bootstrap", idempotency_key="bootstrap-1",
            expected_revision=0, user_id="root", email="root@company.com",
        )
    )
    if delegation is not None:
        users.register(
            ProductionRegistryUserCommand(
                org_id="acme", principal_id="bootstrap", idempotency_key="bootstrap-2",
                expected_revision=1, user_id="alice", email="alice@company.com",
                manager_id="root",
            )
        )
    identity_path = tmp_path / "identity.db"
    SqliteProductionIdentitySessions.migrate(identity_path)
    sessions = SqliteProductionIdentitySessions(
        identity_path, registry=users, configured_org_id="acme",
        provider_id="corp-oidc", issuer="https://id.company.test",
        _identity_session_id_factory=lambda: _SID,
    )
    sessions.establish(
        VerifiedEmailIdentityProof(
            provider_id="corp-oidc", issuer="https://id.company.test",
            email="root@company.com", email_verified=True,
        )
    )
    card_store = None
    card_auth = None
    if cards_enabled:
        SqliteProductionAgentCards.migrate(registry_path)
        card_auth = _CardAuth()
        card_store = SqliteProductionAgentCards(registry_path, authorize=card_auth)
    client = TestClient(create_production_onboarding_app(
        users=users, principal_resolver=ProductionPrincipalResolver(sessions),
        agent_cards=card_store, card_authorizer=card_auth,
        registration_delegation_authorizer=cast(Any, delegation),
    ))
    cast(Any, client).cookies.set("aon_identity_session", _SID)
    return client


def _post(client: TestClient, body: object, key: str = "command-2") -> Response:
    return cast(
        Response,
        cast(Any, client).post(
            "/admin/users", json=body, headers={"Idempotency-Key": key}
        ),
    )


def test_status와_list는_current_user만_verified다(tmp_path: Path) -> None:
    client = _client(tmp_path)
    status = cast(Response, cast(Any, client).get("/onboarding/status"))
    assert status.status_code == 200
    assert status.json()["revision"] == 1
    users = cast(Response, cast(Any, client).get("/admin/users")).json()
    assert users[0]["sso_link_status"] == "verified_email_match"


def test_post는_server_principal_org와_idempotency_header를_사용한다(tmp_path: Path) -> None:
    client = _client(tmp_path)
    body = {
        "expected_revision": 1, "user_id": "alice",
        "email": "alice@company.com", "manager": "root",
    }
    first = _post(client, body)
    replay = _post(client, body)
    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    conflict = _post(client, {**body, "email": "other@company.com"})
    assert conflict.status_code == 409


def test_cookie를_body보다_먼저_검증하고_self_report를_거부한다(tmp_path: Path) -> None:
    client = _client(tmp_path)
    cast(Any, client).cookies.clear()
    malformed = cast(Response, cast(Any, client).post("/admin/users", content=b"{"))
    assert malformed.status_code == 401
    cast(Any, client).cookies.set("aon_identity_session", _SID)
    response = _post(
        client,
        {
            "expected_revision": 1, "user_id": "alice", "email": "alice@company.com",
            "manager": "root", "org_id": "evil", "principal_id": "evil",
        },
    )
    assert response.status_code == 422


def test_production_capability_missing은_body와_registry를_읽기전_503이다() -> None:
    client = TestClient(
        create_production_onboarding_app(users=None, principal_resolver=None)
    )
    responses = (
        cast(Response, cast(Any, client).get("/admin/users")),
        cast(Response, cast(Any, client).get("/onboarding/status")),
        cast(Response, cast(Any, client).post("/admin/users", content=b"{")),
    )
    for response in responses:
        assert response.status_code == 503
        assert "user_id" not in response.text


def test_card_capability_missing은_user_route를_유지하고_body전_503이다(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    assert cast(Response, cast(Any, client).get("/admin/users")).status_code == 200
    response = cast(
        Response, cast(Any, client).post("/admin/agent-cards", content=b"{")
    )
    assert response.status_code == 503


def test_card_live_register_reload와_raw_extra_reject(tmp_path: Path) -> None:
    client = _client(tmp_path, cards_enabled=True)
    body = {
        "expected_revision": 1,
        "agent_id": "support",
        "owner": "root",
        "team": "support",
        "summary": "Support",
        "domains": ["support"],
        "maintainer": None,
        "can_answer": ["refund"],
        "cannot_answer": [],
        "approval_when": [],
        "collaborate_when": [],
        "knowledge_sources": ["okf"],
        "trust_labels": ["internal"],
    }
    bad = cast(Response, cast(Any, client).post(
        "/admin/agent-cards", json={**body, "authority": ["admin"]},
        headers={"Idempotency-Key": "bad"},
    ))
    assert bad.status_code == 422
    first = cast(Response, cast(Any, client).post(
        "/admin/agent-cards", json=body, headers={"Idempotency-Key": "card-1"},
    ))
    assert first.status_code == 200
    assert first.json()["revision"] == 2
    listed = cast(Response, cast(Any, client).get("/admin/agent-cards"))
    assert [row["agent_id"] for row in listed.json()] == ["support"]
    status = cast(Response, cast(Any, client).get("/onboarding/status")).json()
    assert status["steps"][1]["state"] == "complete"
    assert status["steps"][2]["state"] == "current"


def test_other_owner등록은_default_403_write0이고_exact_delegation만_허용한다(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path, cards_enabled=True)
    body = {
        "expected_revision": 1, "agent_id": "support", "owner": "alice",
        "team": "support", "summary": "Support", "domains": ["support"],
    }
    denied = cast(Response, cast(Any, client).post(
        "/admin/agent-cards", json=body, headers={"Idempotency-Key": "denied"},
    ))
    assert denied.status_code == 403
    assert cast(Response, cast(Any, client).get("/admin/agent-cards")).json() == []

    class _ExactDelegation:
        def allows(self, **resource: object) -> bool:
            return (
                resource["org_id"] == "acme"
                and resource["agent_id"] == "support"
                and resource["owner_id"] == "alice"
            )

    admin = _client(tmp_path / "delegated", cards_enabled=True, delegation=_ExactDelegation())
    allowed = cast(Response, cast(Any, admin).post(
        "/admin/agent-cards", json={**body, "expected_revision": 2},
        headers={"Idempotency-Key": "allowed"},
    ))
    assert allowed.status_code == 200


def test_card_body는_server_fields와_string_revision을_reject한다(tmp_path: Path) -> None:
    client = _client(tmp_path, cards_enabled=True)
    base = {
        "expected_revision": 1, "agent_id": "support", "owner": "root",
        "team": "support", "summary": "Support", "domains": ["support"],
    }
    for changes in (
        {"expected_revision": "1"},
        {"last_reviewed_at": "2020-01-01"},
        {"org_id": "evil"},
        {"principal_id": "evil"},
        {"idempotency_key": "evil"},
    ):
        response = cast(Response, cast(Any, client).post(
            "/admin/agent-cards", json={**base, **changes},
            headers={"Idempotency-Key": "strict"},
        ))
        assert response.status_code == 422
