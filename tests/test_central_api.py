"""RB3.1a exact four-route HTTP boundary."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys
import time
from types import SimpleNamespace
from typing import Any, cast

from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from starlette.routing import Route
import yaml
import pytest
import agent_org_network.central_api as central_api_module

from agent_org_network.central_api import create_central_api_app, run_central_api, uvicorn_runner
from agent_org_network.central_browser_auth import BrowserPkceVerifierVault
from agent_org_network.central_browser_auth_sqlite import BrowserSessionCurrentOutcome
from agent_org_network.central_browser_oidc import FakeOidcAuthorizationCodeExchange
from agent_org_network.central_authority import (
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
from agent_org_network.central_bootstrap_sqlite import (
    CentralBootstrapAdminSealStore,
    CentralBootstrapSealUnavailable,
)
from agent_org_network.central_composition import (
    compose_central,
    load_central_installation_config,
    migrate_central_schema,
)
from agent_org_network.central_question_lifecycle import QuestionCreateCommand
from agent_org_network.central_question_request_sqlite import CentralQuestionRequestSqliteUnavailable
from agent_org_network.central_operational_evidence import (
    OperationalEvidenceProjector,
)
from agent_org_network.central_inbox_approval import (
    ApprovalInboxApplication,
    ApprovalItemDetail,
    ApprovalItemSummary,
    ApprovalReadCommand,
)
from agent_org_network.oidc import FakeOidcProvider, OidcClaims


NOW = datetime(2026, 7, 31, 5, 30, tzinfo=UTC)


def _app(
    tmp_path: Path,
    *,
    bootstrap: bool = True,
    oidc_provider: FakeOidcProvider | None = None,
    browser_exchange: FakeOidcAuthorizationCodeExchange | None = None,
    browser_vault: BrowserPkceVerifierVault | None = None,
    browser_random_handle: Any = None,
) -> FastAPI:
    tmp_path.mkdir(parents=True, exist_ok=True)
    policy: dict[str, object] = {
        "schema_version": 1,
        "org_id": "acme",
        "policy_version": "v1",
        "content_sha256": "pending",
        "subject_roles": [
            {"org_id": "acme", "subject_id": "root", "roles": ["requester", "admin"]}
        ],
        "role_permissions": [
            {"role": "requester", "actions": ["question.create", "question.read", "feedback.create"]},
            {
                "role": "admin",
                "actions": [
                    "user.register",
                    "card.register",
                    "session.establish",
                    "session.read",
                    "conflict.list",
                    "conflict.concur",
                    "backup_review.list",
                    "backup_review.read",
                    "backup_review.decide",
                    "reevaluation.list",
                    "reevaluation.read",
                    "reevaluation.decide",
                    "approval.list",
                    "approval.read",
                    "approval.decide",
                    "approval.reassign",
                    "monitor.read",
                    "audit.read",
                    "org_graph.read",
                    "scorecard.organization.read",
                    "card.transfer_owner",
                    "card.revoke",
                    "policy.read",
                    "policy.write",
                ],
            },
        ],
        "route_rules": [],
        "worker_bindings": [],
    }
    policy["content_sha256"] = canonical_policy_digest(policy)
    authority = tmp_path / "authority.yaml"
    authority.write_text(yaml.safe_dump(policy), encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    profile = tmp_path / "central.json"
    profile.write_text(
        json.dumps(
            {
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
                "data_directory": str(data),
                "bind_host": "127.0.0.1",
                "port": 8010,
            }
        ),
        encoding="utf-8",
    )
    config = load_central_installation_config(profile)
    migrate_central_schema(config)
    if bootstrap:
        snapshot = load_authority_policy_yaml(
            authority.read_text(encoding="utf-8"), expected_org_id="acme"
        )
        identity = VerifiedBootstrapIdentity(
            issuer=config.oidc_issuer, audience=config.oidc_audience,
            subject="root-sub", email="root@example.test", email_verified=True,
        )

        class Device:
            def authorize(self, **_kwargs: object) -> VerifiedBootstrapIdentity:
                return identity

        from hashlib import sha256

        def digest(value: str) -> str:
            return sha256(value.encode()).hexdigest()
        BootstrapAdminApplication(
            config=BootstrapAdminConfig(
                org_id=config.org_id, oidc_provider_id=config.oidc_provider_id,
                oidc_issuer=config.oidc_issuer, oidc_audience=config.oidc_audience,
                authority_policy_digest=snapshot.content_sha256,
            ),
            authority=SnapshotCentralAuthorizer(snapshot), device_authorizer=Device(),
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
    oidc = oidc_provider or FakeOidcProvider(
        {
            "valid-token": OidcClaims(
                sub="root-sub",
                email="root@example.test",
                email_verified=True,
                iss="https://idp.example.test",
                aud="aon-central",
            ),
            "unknown-user-token": OidcClaims(
                sub="unknown-sub",
                email="unknown@example.test",
                email_verified=True,
                iss="https://idp.example.test",
                aud="aon-central",
            ),
            "other-org-token": OidcClaims(
                sub="other-sub",
                email="other@example.test",
                email_verified=True,
                iss="https://idp.example.test",
                aud="aon-central",
            ),
        }
    )
    composition = compose_central(
        config,
        oidc_provider=oidc,
        request_id_factory=lambda: "request-1",
        clock=lambda: NOW,
        browser_code_exchange=browser_exchange,
        browser_pkce_vault=browser_vault,
        browser_random_handle=(browser_random_handle or __import__("secrets").token_urlsafe),
    )
    app = create_central_api_app(composition)
    app.state.composition = composition
    return app


def _request(
    client: TestClient,
    method: str,
    path: str,
    *,
    token: str | None = None,
    json_body: object | None = None,
) -> Response:
    http: Any = client
    headers = {} if token is None else {"authorization": f"Bearer {token}"}
    return cast(
        Response,
        http.request(method, path, headers=headers, json=json_body),
    )


def _browser_question_headers(tmp_path: Path) -> tuple[FastAPI, TestClient, dict[str, str]]:
    """Establish one deterministic Browser Session for private question routes."""
    values = iter((
        "transaction-handle-000000000000000001", "state-value-00000000000000000000001",
        "nonce-00000000000000000000000000000001", "verifier-000000000000000000000000001",
        "session-handle-0000000000000000000001", "csrf-token-000000000000000000000000001",
    ))
    exchange = FakeOidcAuthorizationCodeExchange(
        {"code-ok": ("https://idp.example.test", "aon-central-browser", "root@example.test", "subject", "nonce-00000000000000000000000000000001")},
        issuer="https://idp.example.test", audience="aon-central-browser",
    )
    app = _app(tmp_path, browser_exchange=exchange,
        browser_vault=BrowserPkceVerifierVault(clock=lambda: NOW, ttl=timedelta(minutes=5)),
        browser_random_handle=lambda: next(values))
    client = TestClient(app)
    http: Any = client
    start = cast(Response, http.post("/v1/browser-auth/login/start", follow_redirects=False, headers={
        "origin": "https://central.example.test", "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "navigate", "sec-fetch-dest": "document",
    }))
    tx = start.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    callback = cast(Response, http.get(
        "/v1/browser-auth/callback?code=code-ok&state=state-value-00000000000000000000001",
        headers={"cookie": "__Host-aon-central-oidc-tx=" + tx}, follow_redirects=False,
    ))
    cookies = callback.headers.get_list("set-cookie")
    session = next(value.split(";", 1)[0].split("=", 1)[1] for value in cookies if value.startswith("__Host-aon-central-session="))
    csrf = next(value.split(";", 1)[0].split("=", 1)[1] for value in cookies if value.startswith("__Host-aon-central-csrf="))
    return app, client, {
        "cookie": f"__Host-aon-central-session={session}; __Host-aon-central-csrf={csrf}",
        "origin": "https://central.example.test", "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors", "sec-fetch-dest": "empty", "x-aon-csrf": csrf,
        "idempotency-key": "question-1", "content-type": "application/json",
    }


def test_private_question_routes_are_session_derived_and_replay_safe(tmp_path: Path) -> None:
    _app_value, client, headers = _browser_question_headers(tmp_path)
    http: Any = client
    created = cast(Response, http.post("/v1/questions", headers=headers, json={"question": "  질문 원문  "}))
    assert created.status_code == 201
    assert created.json() == {
        "request_id": "request-1", "state": "received",
        "created_at": NOW.isoformat().replace("+00:00", "Z"), "replayed": False,
    }
    replay = cast(Response, http.post("/v1/questions", headers=headers, json={"question": "  질문 원문  "}))
    assert replay.status_code == 201 and replay.json()["replayed"] is True
    current = cast(Response, http.get("/v1/questions/request-1", headers={"cookie": headers["cookie"]}))
    assert current.status_code == 200
    assert current.json() == {
        "type": "pending", "request_id": "request-1", "state": "received", "kind": "routing",
        "retryable": True, "message": "질문을 처리하고 있습니다.",
    }
    stream = cast(Response, http.get("/v1/questions/request-1/stream", headers={
        "cookie": headers["cookie"], "accept": "text/event-stream",
    }))
    assert stream.status_code == 200 and "event: accepted" in stream.text and "event: pending" in stream.text


def test_policy_private_routes_are_strict_and_deny_missing_approval(tmp_path: Path) -> None:
    _app, client, headers = _browser_question_headers(tmp_path)
    http: Any = client
    current = cast(Response, http.get(
        "/v1/admin/policy", headers={"cookie": headers["cookie"]}
    ))
    assert current.status_code == 200
    view = current.json()
    assert view["epoch"] == 1 and view["org_id"] == "acme"
    invalid = cast(Response, http.post(
        "/v1/admin/policy/revisions",
        headers={**headers, "idempotency-key": "policy-http-1"},
        json={
            "kind": "activate", "expected_epoch": view["epoch"],
            "expected_digest": view["policy_digest"],
            "document": view["canonical_document"],
            "approval": {"evidence_id": "missing", "evidence_digest": "a" * 64},
        },
    ))
    assert invalid.status_code == 409
    assert invalid.json() == {"error": "policy_revision_conflict"}
    assert cast(Response, http.get(
        "/v1/admin/policy", headers={"cookie": headers["cookie"]}
    )).json()["epoch"] == 1


def test_central_admin_graph_ownership_and_scorecard_routes_fail_closed_without_v21_capability(
    tmp_path: Path,
) -> None:
    _app, client, headers = _browser_question_headers(tmp_path)
    http: Any = client
    cookie_only = {"cookie": headers["cookie"]}
    assert cast(Response, http.get("/v1/console/org", headers=cookie_only)).json() == {
        "error": "central_admin_unavailable"
    }
    assert cast(Response, http.get(
        "/v1/admin/scorecard?since=2026-08-01T00:00:00Z&until=2026-08-02T00:00:00Z",
        headers=cookie_only,
    )).json() == {"error": "central_admin_unavailable"}
    admitted = cast(Response, http.post(
        "/admin/agent-cards",
        headers={**headers, "idempotency-key": "admin-support-card"},
        json={
            "expected_revision": 1, "agent_id": "support", "owner": "root",
            "team": "Support", "summary": "Support", "domains": ["support"],
            "maintainer": None, "can_answer": [], "cannot_answer": [],
            "approval_when": [], "collaborate_when": [], "knowledge_sources": [],
            "trust_labels": [],
        },
    ))
    assert admitted.status_code == 200
    transfer = cast(Response, http.post(
        "/v1/admin/agent-cards/support/owner-transfers",
        headers={**headers, "idempotency-key": "owner-transfer-1"},
        json={
            "new_owner_user_id": "alice", "expected_card_revision": 1,
            "expected_assignment_generation": 1, "expected_assignment_revision": 1,
        },
    ))
    assert transfer.status_code == 503 and transfer.json() == {"error": "central_admin_unavailable"}
    revoke = cast(Response, http.post(
        "/v1/admin/agent-cards/support/revocations",
        headers={**headers, "idempotency-key": "owner-revoke-1"},
        json={
            "reason_code": "operator_revoke", "expected_card_revision": 1,
            "expected_assignment_generation": 1, "expected_assignment_revision": 1,
        },
    ))
    assert revoke.status_code == 503 and revoke.json() == {"error": "central_admin_unavailable"}
    forged = cast(Response, http.get("/v1/console/org?org=forged", headers=cookie_only))
    assert forged.status_code == 422 and forged.json() == {"error": "invalid_central_admin_request"}


def test_question_greeting_returns_received_receipt_then_declined_projection(tmp_path: Path) -> None:
    _app_value, client, headers = _browser_question_headers(tmp_path)
    http: Any = client
    created = cast(Response, http.post("/v1/questions", headers=headers, json={"question": "안녕하세요"}))
    assert created.status_code == 201 and created.json()["state"] == "received"
    current = cast(Response, http.get("/v1/questions/request-1", headers={"cookie": headers["cookie"]}))
    assert current.status_code == 200
    assert current.json()["type"] == "declined"
    assert current.json()["reason_code"] == "non_actionable_conversation"


def test_product_recovery_routes_unowned_request_after_received_receipt(tmp_path: Path) -> None:
    app, client, headers = _browser_question_headers(tmp_path)
    composition = app.state.composition
    policy = cast(dict[str, object], yaml.safe_load(
        composition.config.authority_snapshot_path.read_text(encoding="utf-8")
    ))
    subject_roles = cast(list[dict[str, object]], policy["subject_roles"])
    roles = cast(list[str], subject_roles[0]["roles"])
    roles.append("manager")
    permissions = cast(list[dict[str, object]], policy["role_permissions"])
    permissions.append({"role": "manager", "actions": ["manager.act"]})
    policy["content_sha256"] = canonical_policy_digest(policy)
    composition.config.authority_snapshot_path.write_text(yaml.safe_dump(policy), encoding="utf-8")
    http: Any = client
    created = cast(Response, http.post("/v1/questions", headers=headers, json={"question": "unknown work"}))
    assert created.status_code == 201 and created.json()["state"] == "received"
    current = cast(Response, http.get("/v1/questions/request-1", headers={"cookie": headers["cookie"]}))
    assert current.status_code == 200
    assert current.json()["state"] == "awaiting_manager"
    assert current.json()["kind"] == "unowned"


def test_product_recovery_uses_canonical_card_and_authority_route_rule(tmp_path: Path) -> None:
    app, client, headers = _browser_question_headers(tmp_path)
    http: Any = client
    card = {
        "expected_revision": 1, "agent_id": "support", "owner": "root", "team": "Support",
        "summary": "Support questions", "domains": ["support"], "maintainer": None,
        "can_answer": [], "cannot_answer": [], "approval_when": [], "collaborate_when": [],
        "knowledge_sources": [], "trust_labels": [],
    }
    admitted = cast(Response, http.post(
        "/admin/agent-cards", headers={**headers, "idempotency-key": "admit-support"}, json=card,
    ))
    assert admitted.status_code == 200
    policy_path = app.state.composition.config.authority_snapshot_path
    policy = cast(dict[str, object], yaml.safe_load(policy_path.read_text(encoding="utf-8")))
    policy["route_rules"] = [{"org_id": "acme", "intent": "support", "agent_card_id": "support"}]
    policy["content_sha256"] = canonical_policy_digest(policy)
    policy_path.write_text(yaml.safe_dump(policy), encoding="utf-8")

    created = cast(Response, http.post(
        "/v1/questions", headers={**headers, "idempotency-key": "route-support"},
        json={"question": "support question"},
    ))
    assert created.status_code == 201 and created.json()["state"] == "received"
    current = cast(Response, http.get("/v1/questions/request-1", headers={"cookie": headers["cookie"]}))
    assert current.status_code == 200
    assert current.json()["state"] == "ready_to_dispatch"
    assert current.json()["kind"] == "routed"


@pytest.mark.parametrize(
    ("case", "question", "expected_state", "expected_kind"),
    (
        ("unowned", "unowned work", "awaiting_manager", "unowned"),
        ("routed", "support work", "ready_to_dispatch", "routed"),
    ),
)
def test_actual_api_lifespan_recovers_seeded_received_once_without_delivery(
    tmp_path: Path, case: str, question: str, expected_state: str, expected_kind: str,
) -> None:
    """Startup recovery owns durable Received rows that never reached post-commit recovery."""
    app, _client, headers = _browser_question_headers(tmp_path)
    composition = app.state.composition
    policy_path = composition.config.authority_snapshot_path
    policy = cast(dict[str, object], yaml.safe_load(policy_path.read_text(encoding="utf-8")))
    if case == "unowned":
        bindings = cast(list[dict[str, object]], policy["subject_roles"])
        cast(list[str], bindings[0]["roles"]).append("manager")
        cast(list[dict[str, object]], policy["role_permissions"]).append(
            {"role": "manager", "actions": ["manager.act"]}
        )
    else:
        http: Any = _client
        admitted = cast(Response, http.post("/admin/agent-cards", headers={
            **headers, "idempotency-key": "admit-support",
        }, json={
            "expected_revision": 1, "agent_id": "support", "owner": "root", "team": "Support",
            "summary": "Support questions", "domains": ["support"], "maintainer": None,
            "can_answer": [], "cannot_answer": [], "approval_when": [], "collaborate_when": [],
            "knowledge_sources": [], "trust_labels": [],
        }))
        assert admitted.status_code == 200
        policy["route_rules"] = [
            {"org_id": "acme", "intent": "support", "agent_card_id": "support"}
        ]
    policy["content_sha256"] = canonical_policy_digest(policy)
    policy_path.write_text(yaml.safe_dump(policy), encoding="utf-8")

    handle = headers["cookie"].split(";", 1)[0].split("=", 1)[1]
    assert composition.browser_auth is not None
    session = composition.browser_auth.get_session(sha256(handle.encode()).hexdigest())
    assert session is not None and composition.question_create is not None
    seeded = composition.question_create.create(QuestionCreateCommand(
        question=question, idempotency_key="seed-received", identity_session_id=session.session_digest,
        expected_org_id="acme", expected_requester_id="root",
    ))
    assert seeded.replayed is False and seeded.request.state.kind == "received"
    with sqlite3.connect(composition.config.database_path) as connection:
        receipt_before = connection.execute(
            "SELECT request_id,question_json,received_json FROM central_question_create_receipts "
            "WHERE org_id=? AND idempotency_key=?", ("acme", "seed-received"),
        ).fetchone()
        assert receipt_before is not None
    # Deliberately omit recovery.recover_one(): this models a crash after the
    # create transaction commit and before its post-commit continuation.
    composition.close()

    restarted = compose_central(composition.config, request_id_factory=lambda: "unused", clock=lambda: NOW)
    restarted_app = create_central_api_app(restarted)
    with TestClient(restarted_app) as restarted_client:
        http = restarted_client
        current = cast(Response, http.get(
            f"/v1/questions/{seeded.request.request_id}", headers={"cookie": headers["cookie"]},
        ))
        assert current.status_code == 200
        assert current.json()["state"] == expected_state
        assert current.json()["kind"] == expected_kind
    with sqlite3.connect(composition.config.database_path) as connection:
        receipt_after = connection.execute(
            "SELECT request_id,question_json,received_json FROM central_question_create_receipts "
            "WHERE org_id=? AND idempotency_key=?", ("acme", "seed-received"),
        ).fetchone()
        assert receipt_after == receipt_before
        assert connection.execute(
            "SELECT COUNT(*) FROM central_question_work_tickets WHERE request_id=?", (seeded.request.request_id,)
        ).fetchone() == (0,)

    # A second real startup is idempotent: recovered states are not routed or
    # delivered again, and the public projection remains unchanged.
    restarted_again = compose_central(composition.config, clock=lambda: NOW)
    with TestClient(create_central_api_app(restarted_again)) as restarted_client:
        second_http: Any = restarted_client
        current = cast(Response, second_http.get(
            f"/v1/questions/{seeded.request.request_id}", headers={"cookie": headers["cookie"]},
        ))
        assert current.status_code == 200
        assert current.json()["state"] == expected_state
        assert current.json()["kind"] == expected_kind


def test_exact_route_graph_and_received_create_read(tmp_path: Path) -> None:
    app, client, headers = _browser_question_headers(tmp_path)
    graph = tuple(
        sorted(
            f"{method} {route.path}"
            for route in app.routes
            if isinstance(route, Route)
            for method in route.methods or ()
        )
    )
    assert graph == (
        "GET /admin/agent-cards",
        "GET /admin/users",
            "GET /healthz",
            "GET /onboarding/status",
            "GET /readyz",
            "GET /v1/admin/policy",
            "GET /v1/admin/scorecard",
            "GET /v1/browser-auth/callback",
            "GET /v1/browser-auth/session",
            "GET /v1/console/audit",
            "GET /v1/console/audit/{audit_id}",
            "GET /v1/console/feed",
            "GET /v1/console/org",
        "GET /v1/inbox/approvals",
        "GET /v1/inbox/approvals/{approval_item_id}",
        "GET /v1/inbox/backup-reviews",
        "GET /v1/inbox/backup-reviews/{review_id}",
        "GET /v1/inbox/conflicts",
        "GET /v1/inbox/conflicts/{case_id}",
        "GET /v1/inbox/reevaluations",
        "GET /v1/inbox/reevaluations/{reevaluation_id}",
        "GET /v1/questions/{request_id}",
        "GET /v1/questions/{request_id}/stream",
            "POST /admin/agent-cards",
            "POST /admin/users",
            "POST /v1/admin/agent-cards/{card_id}/owner-transfers",
            "POST /v1/admin/agent-cards/{card_id}/revocations",
            "POST /v1/admin/policy/revisions",
            "POST /v1/browser-auth/login/start",
            "POST /v1/browser-auth/logout",
        "POST /v1/inbox/approvals/{approval_item_id}/dispositions",
        "POST /v1/inbox/approvals/{approval_item_id}/reassignments",
        "POST /v1/inbox/backup-reviews/{review_id}/dispositions",
        "POST /v1/inbox/conflicts/{case_id}/concurrences",
        "POST /v1/inbox/reevaluations/{reevaluation_id}/dispositions",
        "POST /v1/questions",
        "POST /v1/questions/{request_id}/feedback",
    )
    assert _request(client, "GET", "/readyz").json() == {"status": "ready"}
    http: Any = client
    created = cast(Response, http.post("/v1/questions", headers=headers, json={"question": "내부 원문 질문"}))
    assert created.status_code == 201
    assert created.json() == {
        "request_id": "request-1",
        "state": "received",
        "created_at": NOW.isoformat().replace("+00:00", "Z"),
        "replayed": False,
    }
    assert "내부 원문 질문" not in created.text
    read = cast(Response, http.get("/v1/questions/request-1", headers={"cookie": headers["cookie"]}))
    assert read.status_code == 200
    assert read.json()["request_id"] == created.json()["request_id"]
    assert read.json()["state"] == "received"


def test_console_operational_routes_are_session_bound_redacted_and_cursor_safe(
    tmp_path: Path,
) -> None:
    app, client, headers = _browser_question_headers(tmp_path)
    composition = app.state.composition
    policy_path = composition.config.authority_snapshot_path
    policy = cast(dict[str, object], yaml.safe_load(policy_path.read_text(encoding="utf-8")))
    permissions = cast(list[dict[str, object]], policy["role_permissions"])
    admin = next(value for value in permissions if value["role"] == "admin")
    actions = cast(list[str], admin["actions"])
    actions.extend(action for action in ("monitor.read", "audit.read") if action not in actions)
    policy["content_sha256"] = canonical_policy_digest(policy)
    policy_path.write_text(yaml.safe_dump(policy), encoding="utf-8")
    assert OperationalEvidenceProjector(
        composition.config.database_path, worker_id="console-test-projector", clock=lambda: NOW
    ).drain() >= 1
    http: Any = client
    unauthenticated = cast(Response, http.get("/v1/console/audit"))
    assert unauthenticated.status_code == 401
    assert unauthenticated.json() == {"error": "browser_session_unauthenticated"}
    malformed = cast(Response, http.get(
        "/v1/console/feed", headers={"cookie": headers["cookie"], "accept": "text/event-stream", "last-event-id": "00"}
    ))
    assert malformed.status_code == 422
    resync = cast(Response, http.get(
        "/v1/console/feed", headers={"cookie": headers["cookie"], "accept": "text/event-stream", "last-event-id": "999"}
    ))
    assert resync.status_code == 200 and "event: resync_required" in resync.text
    assert resync.text.count("event: resync_required") == 1
    assert '"code":"resync_required"' in resync.text
    durable_reader = composition.operational_evidence
    assert durable_reader is not None

    class CloseAfterCurrentReader:
        def __init__(self) -> None:
            self.calls = 0

        def feed(self, org_id: str, last_event_id: int | None = None) -> tuple[object, ...]:
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError("closed test stream")
            return durable_reader.feed(org_id, last_event_id)

        def audit_list(self, org_id: str, *, before_cursor: int | None = None, limit: int = 50) -> object:
            return durable_reader.audit_list(org_id, before_cursor=before_cursor, limit=limit)

        def audit_detail(self, org_id: str, audit_id: str) -> object:
            return durable_reader.audit_detail(org_id, audit_id)

    object.__setattr__(composition, "operational_evidence", CloseAfterCurrentReader())
    feed = cast(Response, http.get(
        "/v1/console/feed", headers={"cookie": headers["cookie"], "accept": "text/event-stream", "last-event-id": "0"}
    ))
    assert feed.status_code == 200
    assert feed.headers["cache-control"] == "no-cache, no-transform"
    assert "event: operational_event" in feed.text and "registry_user_registered" in feed.text
    assert "question" not in feed.text and "session-handle" not in feed.text
    object.__setattr__(composition, "operational_evidence", durable_reader)
    listed = cast(Response, http.get("/v1/console/audit?limit=1", headers={"cookie": headers["cookie"]}))
    assert listed.status_code == 200
    payload = listed.json()
    assert payload["oldest_available_cursor"] == 1 and payload["latest_cursor"] >= 1
    assert payload["next_before_cursor"] == payload["items"][0]["cursor"]
    audit_id = payload["items"][0]["record"]["audit_id"]
    detail = cast(Response, http.get(f"/v1/console/audit/{audit_id}", headers={"cookie": headers["cookie"]}))
    assert detail.status_code == 200 and detail.json()["cursor"] == payload["items"][0]["cursor"]
    assert "question" not in detail.text and "session-handle" not in detail.text
    unknown_query = cast(Response, http.get("/v1/console/audit?limit=1&forged=true", headers={"cookie": headers["cookie"]}))
    assert unknown_query.status_code == 422
    missing = cast(Response, http.get("/v1/console/audit/not-present", headers={"cookie": headers["cookie"]}))
    assert missing.status_code == 404 and missing.content == b""
    # A header cannot self-assign the monitoring grant.
    forged = cast(Response, http.get("/v1/console/audit", headers={"cookie": headers["cookie"], "x-aon-role": "admin"}))
    assert forged.status_code == 422
    original_reader = composition.operational_evidence
    assert original_reader is not None
    events = original_reader.feed("acme", 0)

    class RevokingReader:
        def feed(self, _org_id: str, _last_event_id: int | None) -> tuple[object, ...]:
            admin["actions"] = [
                action for action in actions if action not in {"monitor.read", "audit.read"}
            ]
            policy["content_sha256"] = canonical_policy_digest(policy)
            policy_path.write_text(yaml.safe_dump(policy), encoding="utf-8")
            object.__setattr__(
                composition,
                "authority",
                SnapshotCentralAuthorizer(
                    load_authority_policy_yaml(
                        policy_path.read_text(encoding="utf-8"),
                        expected_org_id="acme",
                    )
                ),
            )
            return events[-1:]

    object.__setattr__(composition, "operational_evidence", RevokingReader())
    interrupted = cast(Response, http.get(
        "/v1/console/feed", headers={"cookie": headers["cookie"], "accept": "text/event-stream", "last-event-id": "0"}
    ))
    assert interrupted.status_code == 200 and "event: interrupted" in interrupted.text
    denied_collection = cast(Response, http.get("/v1/console/audit", headers={"cookie": headers["cookie"]}))
    assert denied_collection.status_code == 403
    denied_detail = cast(Response, http.get(f"/v1/console/audit/{audit_id}", headers={"cookie": headers["cookie"]}))
    assert denied_detail.status_code == 404 and denied_detail.content == b""


def test_running_server_projects_question_into_sse_and_audit_without_restart(
    tmp_path: Path,
) -> None:
    bootstrap_app, _bootstrap_client, headers = _browser_question_headers(tmp_path)
    composition = bootstrap_app.state.composition
    policy_path = composition.config.authority_snapshot_path
    policy = cast(dict[str, object], yaml.safe_load(policy_path.read_text(encoding="utf-8")))
    permissions = cast(list[dict[str, object]], policy["role_permissions"])
    admin = next(value for value in permissions if value["role"] == "admin")
    initial_actions = cast(list[str], admin["actions"])
    initial_actions.extend(action for action in ("monitor.read", "audit.read") if action not in initial_actions)
    policy["content_sha256"] = canonical_policy_digest(policy)
    policy_path.write_text(yaml.safe_dump(policy), encoding="utf-8")

    app = create_central_api_app(
        composition,
        operational_poll_seconds=0.01,
        sse_poll_seconds=0.01,
        sse_keepalive_seconds=0.05,
    )
    cookie_values = dict(
        pair.split("=", 1) for pair in headers["cookie"].split("; ")
    )

    class ConnectedRequest:
        cookies = cookie_values

        async def is_disconnected(self) -> bool:
            return False

    with TestClient(app) as client:
        http: Any = client
        reader = composition.operational_evidence
        assert reader is not None
        _items, _oldest, high_water, _next = reader.audit_list("acme", limit=1)
        assert high_water >= 1

        async def capture_after_transition() -> tuple[Response, str]:
            stream = central_api_module._operational_event_frames(  # pyright: ignore[reportPrivateUsage]
                composition,
                cast(Any, ConnectedRequest()),
                reader=reader,
                initial_cursor=high_water,
                initial_events=(),
                runtime=app.state.operational_runtime,
                poll_seconds=0.01,
                keepalive_seconds=0.05,
            )

            async def wait_for_question() -> str:
                async for frame in stream:
                    if (
                        "event: operational_event" in frame
                        and '"event_type":"question_received"' in frame
                    ):
                        return frame
                raise AssertionError("SSE closed before the projected question")

            waiting = asyncio.create_task(wait_for_question())
            try:
                # Establish the stream first, then commit the source transition
                # through the running Central HTTP boundary.
                await asyncio.sleep(0.03)
                created = cast(Response, http.post(
                    "/v1/questions", headers=headers, json={"question": "project me safely"}
                ))
                frame = await asyncio.wait_for(waiting, timeout=2.0)
                return created, frame
            finally:
                if not waiting.done():
                    waiting.cancel()
                await cast(Any, stream).aclose()

        created, frame = asyncio.run(capture_after_transition())
        assert created.status_code == 201
        assert '"request_id":"request-1"' in frame
        assert "project me safely" not in frame
        assert app.state.operational_runtime.ready is True

        audit: Response | None = None
        for _attempt in range(100):
            candidate = cast(Response, http.get(
                "/v1/console/audit?limit=100",
                headers={"cookie": headers["cookie"]},
            ))
            if candidate.status_code == 200 and any(
                item["record"]["action"] == "question.create"
                and item["record"]["resource"]["resource_id"] == "request-1"
                for item in candidate.json()["items"]
            ):
                audit = candidate
                break
            time.sleep(0.01)
        assert audit is not None
        assert "project me safely" not in audit.text


def test_console_operational_catalog_drift_is_safe_unavailable(tmp_path: Path) -> None:
    app, client, headers = _browser_question_headers(tmp_path)
    composition = app.state.composition
    policy_path = composition.config.authority_snapshot_path
    policy = cast(dict[str, object], yaml.safe_load(policy_path.read_text(encoding="utf-8")))
    permissions = cast(list[dict[str, object]], policy["role_permissions"])
    admin = next(value for value in permissions if value["role"] == "admin")
    if "audit.read" not in cast(list[str], admin["actions"]):
        cast(list[str], admin["actions"]).append("audit.read")
    policy["content_sha256"] = canonical_policy_digest(policy)
    policy_path.write_text(yaml.safe_dump(policy), encoding="utf-8")
    # The reader validates its owned catalog on every read; this is neither a
    # best-effort row parse nor an in-memory fallback.
    with sqlite3.connect(composition.config.database_path) as connection:
        connection.execute("ALTER TABLE central_operational_events ADD COLUMN forged TEXT")
    http: Any = client
    response = cast(Response, http.get("/v1/console/audit", headers={"cookie": headers["cookie"]}))
    assert response.status_code == 503
    assert response.json() == {"error": "operational_evidence_unavailable"}
    assert "forged" not in response.text and "central.sqlite" not in response.text


def test_operational_projector_lifespan_fault_and_idle_shutdown_are_bounded(
    tmp_path: Path,
) -> None:
    base = _app(tmp_path / "idle", bootstrap=False)
    composition = base.state.composition

    class IdleProjector:
        def __init__(self) -> None:
            self.drain_calls = 0
            self.project_calls = 0

        def drain(self) -> int:
            self.drain_calls += 1
            return 0

        def project_one(self) -> bool:
            self.project_calls += 1
            return False

    idle = IdleProjector()
    object.__setattr__(composition, "operational_evidence_projector", idle)
    app = create_central_api_app(composition, operational_poll_seconds=0.02)
    with TestClient(app):
        time.sleep(0.075)
        assert app.state.operational_runtime.ready is True
        assert idle.drain_calls == 1
        # A no-work projector sleeps between polls; it cannot spin tightly.
        assert 1 <= idle.project_calls <= 6
    calls_after_shutdown = idle.project_calls
    time.sleep(0.04)
    assert idle.project_calls == calls_after_shutdown

    fault_base = _app(tmp_path / "fault", bootstrap=False)
    fault_composition = fault_base.state.composition

    class FaultProjector:
        def drain(self) -> int:
            return 0

        def project_one(self) -> bool:
            raise RuntimeError("projector fault detail must stay private")

    object.__setattr__(fault_composition, "operational_evidence_projector", FaultProjector())
    fault_app = create_central_api_app(fault_composition, operational_poll_seconds=0.01)
    with TestClient(fault_app) as client:
        http: Any = client
        for _attempt in range(20):
            if fault_app.state.operational_runtime.ready is False:
                break
            time.sleep(0.01)
        assert fault_app.state.operational_runtime.ready is False
        response = cast(Response, http.get("/readyz"))
        assert response.status_code == 503
        assert "projector fault detail" not in response.text

    drain_fault_base = _app(tmp_path / "drain-fault", bootstrap=False)
    drain_fault_composition = drain_fault_base.state.composition

    class DrainFaultProjector:
        def __init__(self) -> None:
            self.project_calls = 0

        def drain(self) -> int:
            raise RuntimeError("startup drain fault detail must stay private")

        def project_one(self) -> bool:
            self.project_calls += 1
            return False

    drain_fault = DrainFaultProjector()
    object.__setattr__(
        drain_fault_composition, "operational_evidence_projector", drain_fault
    )
    drain_fault_app = create_central_api_app(drain_fault_composition)
    with TestClient(drain_fault_app) as client:
        http = client
        assert drain_fault_app.state.operational_runtime.ready is False
        response = cast(Response, http.get("/readyz"))
        assert response.status_code == 503
        assert "startup drain fault detail" not in response.text
        assert drain_fault.project_calls == 0


def test_operational_sse_disconnect_stops_polling_without_busy_loop(tmp_path: Path) -> None:
    app = _app(tmp_path, bootstrap=False)
    composition = app.state.composition
    durable_reader = composition.operational_evidence
    assert durable_reader is not None

    class CountingReader:
        def __init__(self) -> None:
            self.calls = 0

        def feed(self, org_id: str, last_event_id: int | None = None) -> tuple[object, ...]:
            self.calls += 1
            return durable_reader.feed(org_id, last_event_id)

    class DisconnectingRequest:
        def __init__(self) -> None:
            self.checks = 0

        async def is_disconnected(self) -> bool:
            self.checks += 1
            return self.checks >= 4

    reader = CountingReader()
    request = DisconnectingRequest()

    async def consume() -> list[str]:
        frames: list[str] = []
        stream = central_api_module._operational_event_frames(  # pyright: ignore[reportPrivateUsage]
            composition,
            cast(Any, request),
            reader=cast(Any, reader),
            initial_cursor=0,
            initial_events=(),
            runtime=app.state.operational_runtime,
            poll_seconds=0.02,
            keepalive_seconds=1.0,
        )
        async for frame in stream:
            frames.append(frame)
        return frames

    started = time.monotonic()
    assert asyncio.run(consume()) == []
    elapsed = time.monotonic() - started
    assert elapsed >= 0.035
    assert 1 <= reader.calls <= 3
    calls_after_disconnect = reader.calls
    time.sleep(0.04)
    assert reader.calls == calls_after_disconnect


def test_operational_sse_fresh_connection_starts_after_durable_high_water(
    tmp_path: Path,
) -> None:
    app, client, headers = _browser_question_headers(tmp_path)
    composition = app.state.composition
    policy_path = composition.config.authority_snapshot_path
    policy = cast(dict[str, object], yaml.safe_load(policy_path.read_text(encoding="utf-8")))
    permissions = cast(list[dict[str, object]], policy["role_permissions"])
    admin = next(value for value in permissions if value["role"] == "admin")
    if "monitor.read" not in cast(list[str], admin["actions"]):
        cast(list[str], admin["actions"]).append("monitor.read")
    policy["content_sha256"] = canonical_policy_digest(policy)
    policy_path.write_text(yaml.safe_dump(policy), encoding="utf-8")
    projector = OperationalEvidenceProjector(
        composition.config.database_path,
        worker_id="fresh-cursor-test-projector",
        clock=lambda: NOW,
    )
    assert projector.drain() >= 1
    durable_reader = composition.operational_evidence
    assert durable_reader is not None
    _items, _oldest, latest, _next = durable_reader.audit_list("acme", limit=1)
    observed_cursors: list[int | None] = []

    class CloseAtHighWaterReader:
        def audit_list(
            self, org_id: str, *, before_cursor: int | None = None, limit: int = 50
        ) -> object:
            return durable_reader.audit_list(
                org_id, before_cursor=before_cursor, limit=limit
            )

        def feed(
            self, _org_id: str, last_event_id: int | None = None
        ) -> tuple[object, ...]:
            observed_cursors.append(last_event_id)
            raise RuntimeError("close fresh test stream")

    object.__setattr__(composition, "operational_evidence", CloseAtHighWaterReader())
    http: Any = client
    response = cast(Response, http.get(
        "/v1/console/feed",
        headers={"cookie": headers["cookie"], "accept": "text/event-stream"},
    ))
    assert response.status_code == 200
    assert observed_cursors == [latest]
    assert "event: operational_event" not in response.text


def test_registry_user_admission_is_session_bound_csrf_protected_and_replay_safe(
    tmp_path: Path,
) -> None:
    values = iter((
        "transaction-handle-000000000000000001", "state-value-00000000000000000000001",
        "nonce-00000000000000000000000000000001", "verifier-000000000000000000000000001",
        "session-handle-0000000000000000000001", "csrf-token-000000000000000000000000001",
    ))
    exchange = FakeOidcAuthorizationCodeExchange(
        {"code-ok": ("https://idp.example.test", "aon-central-browser", "root@example.test", "subject", "nonce-00000000000000000000000000000001")},
        issuer="https://idp.example.test", audience="aon-central-browser",
    )
    app = _app(
        tmp_path, browser_exchange=exchange,
        browser_vault=BrowserPkceVerifierVault(clock=lambda: NOW, ttl=timedelta(minutes=5)),
        browser_random_handle=lambda: next(values),
    )
    client = TestClient(app)
    http: Any = client
    start = cast(Response, http.post("/v1/browser-auth/login/start", follow_redirects=False, headers={
        "origin": "https://central.example.test", "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "navigate", "sec-fetch-dest": "document",
    }))
    tx = start.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    callback = cast(Response, http.get(
        "/v1/browser-auth/callback?code=code-ok&state=state-value-00000000000000000000001",
        headers={"cookie": "__Host-aon-central-oidc-tx=" + tx}, follow_redirects=False,
    ))
    cookies = callback.headers.get_list("set-cookie")
    session = next(value.split(";", 1)[0].split("=", 1)[1] for value in cookies if value.startswith("__Host-aon-central-session="))
    csrf = next(value.split(";", 1)[0].split("=", 1)[1] for value in cookies if value.startswith("__Host-aon-central-csrf="))
    cookie = f"__Host-aon-central-session={session}; __Host-aon-central-csrf={csrf}"
    headers = {
        "cookie": cookie, "origin": "https://central.example.test",
        "sec-fetch-site": "same-origin", "sec-fetch-mode": "cors", "sec-fetch-dest": "empty",
        "x-aon-csrf": csrf, "idempotency-key": "admit-user-1", "content-type": "application/json",
    }
    command = {"expected_revision": 1, "user_id": "second", "email": "second@example.test", "manager": "root"}
    created = cast(Response, http.post("/admin/users", headers=headers, json=command))
    assert created.status_code == 200
    assert created.headers["cache-control"] == "no-store"
    assert created.json() == {
        "user_id": "second", "email": "second@example.test", "manager": "root",
        "revision": 2, "replayed": False,
    }
    replay = cast(Response, http.post("/admin/users", headers=headers, json=command))
    assert replay.status_code == 200 and replay.json() == {**created.json(), "replayed": True}
    card_command = {
        "expected_revision": 2, "agent_id": "support", "owner": "root", "team": "Support",
        "summary": "Support questions", "domains": ["support"], "maintainer": None,
        "can_answer": [], "cannot_answer": [], "approval_when": [], "collaborate_when": [],
        "knowledge_sources": [], "trust_labels": [],
    }
    card_created = cast(Response, http.post(
        "/admin/agent-cards", headers={**headers, "idempotency-key": "admit-card-1"}, json=card_command,
    ))
    assert card_created.status_code == 200
    assert card_created.json() == {
        "card": {
            "agent_id": "support", "owner": "root", "team": "Support",
            "summary": "Support questions", "domains": ["support"],
            "last_reviewed_at": "2026-07-31", "maintainer": None,
            "can_answer": [], "cannot_answer": [], "approval_when": [], "collaborate_when": [],
            "knowledge_sources": [], "trust_labels": [],
        },
        "revision": 3, "replayed": False,
    }
    card_replay = cast(Response, http.post(
        "/admin/agent-cards", headers={**headers, "idempotency-key": "admit-card-1"}, json=card_command,
    ))
    assert card_replay.status_code == 200
    assert card_replay.json() == {**card_created.json(), "replayed": True}
    cards = cast(Response, http.get("/admin/agent-cards", headers={"cookie": cookie}))
    assert cards.status_code == 200
    assert cards.json() == [card_created.json()["card"]]
    invalid_owner = cast(Response, http.post(
        "/admin/agent-cards", headers={**headers, "idempotency-key": "invalid-card-owner"},
        json={**card_command, "expected_revision": 3, "agent_id": "bad-owner", "owner": "ghost"},
    ))
    assert invalid_owner.status_code == 422
    assert invalid_owner.json() == {"error": "invalid_registration_request"}
    unknown_claim = cast(Response, http.post(
        "/admin/agent-cards", headers={**headers, "idempotency-key": "forged-card-claim", "x-aon-role": "admin"},
        json={**card_command, "expected_revision": 3, "agent_id": "forged-card"},
    ))
    assert unknown_claim.status_code == 403
    assert unknown_claim.json() == {"error": "browser_csrf_forbidden"}
    composition = app.state.composition
    assert composition.registry is not None
    assert composition.registry.counts("acme") == {"receipts": 2, "audit": 2, "outbox": 2}
    users = cast(Response, http.get("/admin/users", headers={"cookie": cookie}))
    assert users.status_code == 200
    assert users.json() == [
        {"user_id": "root", "email": "root@example.test", "manager": None, "sso_link_status": "verified_email_match"},
        {"user_id": "second", "email": "second@example.test", "manager": "root", "sso_link_status": "unlinked"},
    ]
    status = cast(Response, http.get("/onboarding/status", headers={"cookie": cookie}))
    assert status.status_code == 200
    assert status.json() == {
        "revision": 3, "card_capability": "available",
        "cards": [{"agent_id": "support", "owner": "root", "team": "Support", "summary": "Support questions"}],
        "card_owner_installation": {"artifact": "agent-org-owner", "href": "/onboarding#card-owner-installation"},
        "steps": [
            {"kind": "user", "label": "Registry User", "state": "complete"},
            {"kind": "card", "label": "Agent Card", "state": "complete"},
            {"kind": "card_owner_installation", "label": "Card Owner Installation", "state": "current"},
        ],
    }
    bad_csrf = cast(Response, http.post("/admin/users", headers={**headers, "x-aon-csrf": "wrong"}, json=command))
    assert bad_csrf.status_code == 403 and bad_csrf.json() == {"error": "browser_csrf_forbidden"}
    missing_dest = cast(Response, http.post(
        "/admin/users", headers={key: value for key, value in headers.items() if key != "sec-fetch-dest"}, json=command
    ))
    assert missing_dest.status_code == 403 and missing_dest.json() == {"error": "browser_csrf_forbidden"}
    self_claim = cast(Response, http.post("/admin/users", headers={**headers, "x-aon-user": "forged"}, json=command))
    assert self_claim.status_code == 403 and self_claim.json() == {"error": "browser_csrf_forbidden"}
    changed = cast(Response, http.post("/admin/users", headers=headers, json={**command, "email": "other@example.test"}))
    assert changed.status_code == 409 and changed.json() == {"error": "registry_registration_conflict"}
    composition = app.state.composition
    factory_builder = composition.registry_admission_factory
    assert factory_builder is not None

    class _EndDuringUserUow:
        def __init__(self, inner: object) -> None:
            self._inner = inner

        def read_current(self, *, action: str) -> object:
            return cast(Any, self._inner).read_current(action=action)

        def create(self) -> object:
            application = cast(Any, self._inner).create()

            def end_after_insert(point: str) -> None:
                if point == "after_user":
                    application.users._connection.execute(
                        "UPDATE browser_sessions SET ended_at=?,terminal_reason='logout' WHERE session_digest=?",
                        (NOW.isoformat(), sha256(session.encode()).hexdigest()),
                    )

            application.users._fault = end_after_insert
            return application

    def end_during_uow_factory(digest: str) -> Any:
        return _EndDuringUserUow(factory_builder(digest))

    object.__setattr__(composition, "registry_admission_factory", end_during_uow_factory)
    session_lost = cast(Response, http.post(
        "/admin/users", headers={**headers, "idempotency-key": "end-during-uow"},
        json={"expected_revision": 3, "user_id": "never", "email": "never@example.test", "manager": "root"},
    ))
    assert session_lost.status_code == 401
    assert session_lost.json() == {"error": "browser_session_unauthenticated"}
    assert composition.registry is not None
    assert composition.registry.revision("acme") == 3
    assert composition.registry.counts("acme") == {"receipts": 2, "audit": 2, "outbox": 2}


def test_registry_user_post_envelope_preserves_session_before_body_precedence(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path))
    http: Any = client
    browser_headers = {
        "origin": "https://central.example.test", "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors", "sec-fetch-dest": "empty",
    }
    no_session_bad_type = cast(Response, http.post(
        "/admin/users", headers={**browser_headers, "content-type": "text/plain"}, content=b"not-json",
    ))
    assert no_session_bad_type.status_code == 401
    assert no_session_bad_type.json() == {"error": "browser_session_unauthenticated"}
    no_session_oversize = cast(Response, http.post(
        "/admin/users", headers={**browser_headers, "content-type": "application/json", "content-length": "65537"}, content=b"{}",
    ))
    assert no_session_oversize.status_code == 401
    assert no_session_oversize.json() == {"error": "browser_session_unauthenticated"}
    invalid_origin = cast(Response, http.post(
        "/admin/users", headers={**browser_headers, "origin": "https://evil.example", "content-type": "text/plain"}, content=b"not-json",
    ))
    assert invalid_origin.status_code == 403
    assert invalid_origin.json() == {"error": "browser_csrf_forbidden"}


def test_browser_oidc_routes_fail_closed_on_origin_and_callback_shape(tmp_path: Path) -> None:
    client = TestClient(_app(
        tmp_path,
        browser_vault=BrowserPkceVerifierVault(
            clock=lambda: NOW,
            ttl=timedelta(minutes=5),
        ),
    ))
    http: Any = client
    missing_origin = cast(Response, http.post("/v1/browser-auth/login/start"))
    assert missing_origin.json() == {
        "error": "browser_origin_forbidden"
    }
    started = cast(Response, http.post(
        "/v1/browser-auth/login/start",
        follow_redirects=False,
        headers={
            "origin": "https://central.example.test",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "navigate",
            "sec-fetch-dest": "document",
        },
    ))
    assert started.status_code == 303
    assert started.headers["location"].startswith(
        "https://idp.example.test/authorize?"
    )
    transaction_cookie = started.headers["set-cookie"]
    assert transaction_cookie.startswith("__Host-aon-central-oidc-tx=")
    assert all(
        attribute in transaction_cookie
        for attribute in ("Path=/", "HttpOnly", "Secure", "SameSite=lax")
    )
    cors_start = cast(Response, http.post(
        "/v1/browser-auth/login/start",
        headers={
            "origin": "https://central.example.test",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
        },
    ))
    assert cors_start.status_code == 403
    assert cors_start.json() == {"error": "browser_origin_forbidden"}
    invalid = cast(Response, http.get(
        "/v1/browser-auth/callback?code=raw-code-topsecret&state=raw-state-topsecret&return_to=https://attacker.test"
    ))
    assert invalid.status_code == 400
    assert invalid.json() == {"error": "browser_oidc_callback_invalid"}
    assert "raw-code-topsecret" not in invalid.text
    assert "raw-state-topsecret" not in invalid.text
    assert "Max-Age=0" in invalid.headers["set-cookie"]


def test_composed_browser_oidc_round_trip_is_cookie_only_and_replay_safe(tmp_path: Path) -> None:
    values = iter((
        "transaction-handle-000000000000000001", "state-value-00000000000000000000001",
        "nonce-00000000000000000000000000000001", "verifier-000000000000000000000000001",
        "session-handle-0000000000000000000001", "csrf-token-000000000000000000000000001",
    ))
    exchange = FakeOidcAuthorizationCodeExchange(
        {"code-ok": ("https://idp.example.test", "aon-central-browser", "root@example.test", "subject", "nonce-00000000000000000000000000000001")},
        issuer="https://idp.example.test", audience="aon-central-browser",
    )
    app = _app(
        tmp_path, browser_exchange=exchange,
        browser_vault=BrowserPkceVerifierVault(clock=lambda: NOW, ttl=timedelta(minutes=5)),
        browser_random_handle=lambda: next(values),
    )
    client = TestClient(app)
    http: Any = client
    start = cast(Response, http.post("/v1/browser-auth/login/start", follow_redirects=False, headers={
        "origin": "https://central.example.test", "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "navigate", "sec-fetch-dest": "document",
    }))
    assert start.status_code == 303 and start.headers["location"].startswith("https://idp.example.test/authorize?")
    tx = start.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    callback = cast(Response, http.get(
        "/v1/browser-auth/callback?code=code-ok&state=state-value-00000000000000000000001",
        headers={"cookie": "__Host-aon-central-oidc-tx=" + tx}, follow_redirects=False,
    ))
    assert callback.status_code == 303 and callback.headers["location"] == "/ask"
    cookies = callback.headers.get_list("set-cookie")
    assert any("__Host-aon-central-oidc-tx=" in item and "Max-Age=0" in item for item in cookies)
    assert any("__Host-aon-central-session=" in item and "HttpOnly" in item and "Secure" in item for item in cookies)
    assert any("__Host-aon-central-csrf=" in item and "SameSite=strict" in item for item in cookies)
    composition = app.state.composition
    assert composition.browser_auth is not None
    assert composition.browser_auth.get_session(sha256("session-handle-0000000000000000000001".encode()).hexdigest()) is not None
    replay = cast(Response, http.get(
        "/v1/browser-auth/callback?code=code-ok&state=state-value-00000000000000000000001",
        headers={"cookie": "__Host-aon-central-oidc-tx=" + tx}, follow_redirects=False,
    ))
    assert replay.status_code == 400 and replay.json() == {"error": "browser_oidc_callback_invalid"}


def test_browser_session_current_and_logout_are_cookie_bound_and_monotonic(tmp_path: Path) -> None:
    values = iter((
        "transaction-handle-000000000000000001", "state-value-00000000000000000000001",
        "nonce-00000000000000000000000000000001", "verifier-000000000000000000000000001",
        "session-handle-0000000000000000000001", "csrf-token-000000000000000000000000001",
    ))
    exchange = FakeOidcAuthorizationCodeExchange(
        {"code-ok": ("https://idp.example.test", "aon-central-browser", "root@example.test", "subject", "nonce-00000000000000000000000000000001")},
        issuer="https://idp.example.test", audience="aon-central-browser",
    )
    app = _app(
        tmp_path, browser_exchange=exchange,
        browser_vault=BrowserPkceVerifierVault(clock=lambda: NOW, ttl=timedelta(minutes=5)),
        browser_random_handle=lambda: next(values),
    )
    client = TestClient(app)
    http: Any = client
    start = cast(Response, http.post("/v1/browser-auth/login/start", follow_redirects=False, headers={
        "origin": "https://central.example.test", "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "navigate", "sec-fetch-dest": "document",
    }))
    tx = start.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    callback = cast(Response, http.get(
        "/v1/browser-auth/callback?code=code-ok&state=state-value-00000000000000000000001",
        headers={"cookie": "__Host-aon-central-oidc-tx=" + tx}, follow_redirects=False,
    ))
    cookies = callback.headers.get_list("set-cookie")
    session = next(value.split(";", 1)[0].split("=", 1)[1] for value in cookies if value.startswith("__Host-aon-central-session="))
    csrf = next(value.split(";", 1)[0].split("=", 1)[1] for value in cookies if value.startswith("__Host-aon-central-csrf="))
    cookie = f"__Host-aon-central-session={session}; __Host-aon-central-csrf={csrf}"
    # Current/end are not coupled to a still-composable IdP code-exchange adapter.
    object.__setattr__(app.state.composition, "browser_oidc", None)

    current = cast(Response, http.get("/v1/browser-auth/session", headers={"cookie": cookie}))
    assert current.status_code == 200
    assert current.headers["cache-control"] == "no-store"
    assert current.json() == {
        "authenticated": True, "registry_user_ref": "root",
        "expires_at": (NOW + timedelta(hours=8)).isoformat().replace("+00:00", "Z"),
        "actions": ["session.read"],
    }
    assert session not in current.text and csrf not in current.text and "root@example.test" not in current.text
    policy_path = app.state.composition.config.authority_snapshot_path
    policy = cast(dict[str, object], yaml.safe_load(policy_path.read_text(encoding="utf-8")))
    permissions = cast(list[dict[str, object]], policy["role_permissions"])
    permissions[1]["actions"] = ["user.register", "session.establish"]
    policy["content_sha256"] = canonical_policy_digest(policy)
    policy_path.write_text(yaml.safe_dump(policy), encoding="utf-8")
    policy_denied = cast(Response, http.get("/v1/browser-auth/session", headers={"cookie": cookie}))
    assert policy_denied.status_code == 403 and policy_denied.json() == {"error": "browser_session_forbidden"}
    policy_path.write_text("not: [valid", encoding="utf-8")
    policy_unavailable = cast(Response, http.get("/v1/browser-auth/session", headers={"cookie": cookie}))
    assert policy_unavailable.status_code == 503 and policy_unavailable.json() == {"error": "browser_session_unavailable"}
    policy["role_permissions"] = [
        {"role": "requester", "actions": ["question.create", "question.read"]},
        {"role": "admin", "actions": ["user.register", "session.establish", "session.read"]},
    ]
    policy["content_sha256"] = canonical_policy_digest(policy)
    policy_path.write_text(yaml.safe_dump(policy), encoding="utf-8")
    composition = app.state.composition
    assert composition.browser_auth is not None
    first_digest = sha256(session.encode()).hexdigest()
    first = composition.browser_auth.get_session(first_digest)
    assert first is not None
    other_handle = "other-session-handle-00000000000000000001"
    other_csrf = "other-csrf-token-000000000000000000000001"
    other = replace(
        first,
        session_digest=sha256(other_handle.encode()).hexdigest(),
        csrf_digest=sha256(other_csrf.encode()).hexdigest(),
    )
    with sqlite3.connect(composition.config.database_path) as connection:
        connection.execute(
            "INSERT INTO browser_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                other.session_digest, other.registry_user_id, other.org_id,
                other.oidc_identity_binding_digest, other.csrf_digest, other.registry_fingerprint,
                other.registry_revision, other.established_at.isoformat(), other.expires_at.isoformat(),
                None, None,
            ),
        )
        connection.execute(
            "UPDATE browser_sessions SET registry_revision=999 WHERE session_digest=?", (first_digest,)
        )
    registry_drift = cast(Response, http.get("/v1/browser-auth/session", headers={"cookie": cookie}))
    assert registry_drift.status_code == 401 and registry_drift.json() == {"error": "browser_session_unauthenticated"}
    with sqlite3.connect(composition.config.database_path) as connection:
        connection.execute(
            "UPDATE browser_sessions SET registry_revision=? WHERE session_digest=?", (first.registry_revision, first_digest)
        )
    assert composition.browser_auth.get_session(first_digest) == first
    assert composition.browser_auth.read_current_session(first_digest, now=NOW)[0] is BrowserSessionCurrentOutcome.ACTIVE
    with sqlite3.connect(composition.config.database_path) as connection:
        connection.execute("ALTER TABLE production_registry_users ADD COLUMN forged TEXT")
    assert composition.browser_auth.read_current_session(first_digest, now=NOW)[0] is BrowserSessionCurrentOutcome.UNAVAILABLE
    registry_unavailable = cast(Response, http.get("/v1/browser-auth/session", headers={"cookie": cookie}))
    assert registry_unavailable.status_code == 503 and registry_unavailable.json() == {"error": "browser_session_unavailable"}
    for response in (policy_denied, policy_unavailable, registry_drift, registry_unavailable):
        assert session not in response.text and csrf not in response.text and "root@example.test" not in response.text
    for header in ("authorization", "x-aon-user", "x-aon-org", "x-aon-role", "x-aon-permission", "x-aon-token-claim"):
        forged = cast(Response, http.get("/v1/browser-auth/session", headers={"cookie": cookie, header: "forged"}))
        assert forged.status_code == 403 and forged.json() == {"error": "browser_session_forbidden"}
    queried = cast(Response, http.get("/v1/browser-auth/session?user=forged", headers={"cookie": cookie}))
    assert queried.status_code == 403 and queried.json() == {"error": "browser_session_forbidden"}
    body = cast(Response, http.request("GET", "/v1/browser-auth/session", content=b"forged", headers={"cookie": cookie}))
    assert body.status_code == 403 and body.json() == {"error": "browser_session_forbidden"}

    denied = cast(Response, http.post("/v1/browser-auth/logout", headers={
        "cookie": cookie, "origin": "https://evil.example", "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors", "sec-fetch-dest": "empty", "x-aon-csrf": csrf,
    }))
    assert denied.status_code == 403 and denied.json() == {"error": "browser_csrf_forbidden"}
    missing_csrf = cast(Response, http.post("/v1/browser-auth/logout", headers={
        "cookie": cookie, "origin": "https://central.example.test", "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors", "sec-fetch-dest": "empty",
    }))
    assert missing_csrf.status_code == 403 and missing_csrf.json() == {"error": "browser_csrf_forbidden"}
    for invalid_headers in (
        {"sec-fetch-site": "same-site"}, {"sec-fetch-mode": "navigate"},
        {"sec-fetch-dest": "document"}, {"authorization": "Bearer forged"},
        {"x-aon-user": "forged"}, {"x-aon-csrf": "wrong"},
    ):
        headers = {
            "cookie": cookie, "origin": "https://central.example.test", "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors", "sec-fetch-dest": "empty", "x-aon-csrf": csrf,
        }
        headers.update(invalid_headers)
        invalid_logout = cast(Response, http.post("/v1/browser-auth/logout", headers=headers))
        assert invalid_logout.status_code == 403 and invalid_logout.json() == {"error": "browser_csrf_forbidden"}
    query_logout = cast(Response, http.post("/v1/browser-auth/logout?claim=forged", headers={
        "cookie": cookie, "origin": "https://central.example.test", "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors", "sec-fetch-dest": "empty", "x-aon-csrf": csrf,
    }))
    assert query_logout.status_code == 403 and query_logout.json() == {"error": "browser_csrf_forbidden"}
    # Local cleanup must not wait for an unavailable policy or Registry component.
    app.state.composition.config.authority_snapshot_path.write_text("not: [valid", encoding="utf-8")
    logout = cast(Response, http.post("/v1/browser-auth/logout", headers={
        "cookie": cookie, "origin": "https://central.example.test", "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors", "sec-fetch-dest": "empty", "x-aon-csrf": csrf,
    }))
    assert logout.status_code == 204 and logout.headers["cache-control"] == "no-store"
    logout_cookies = logout.headers.get_list("set-cookie")
    assert len(logout_cookies) == 3
    assert all("Max-Age=0" in value and "Secure" in value and "Path=/" in value for value in logout_cookies)
    assert any("HttpOnly" in value and "SameSite=lax" in value for value in logout_cookies)
    assert any("HttpOnly" not in value and "SameSite=strict" in value for value in logout_cookies)
    assert cast(Response, http.get("/v1/browser-auth/session", headers={"cookie": cookie})).json() == {
        "error": "browser_session_unauthenticated"
    }
    other_still_active = composition.browser_auth.get_session(other.session_digest)
    assert other_still_active is not None and other_still_active.ended_at is None
    repeat = cast(Response, http.post("/v1/browser-auth/logout", headers={
        "cookie": cookie, "origin": "https://central.example.test", "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors", "sec-fetch-dest": "empty", "x-aon-csrf": csrf,
    }))
    assert repeat.status_code == 204


def test_uvicorn_runner_disables_raw_access_logging(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def run(app: object, **kwargs: object) -> None:
        seen.update(app=app, **kwargs)

    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=run))
    app = _app(tmp_path)
    uvicorn_runner(app, host="127.0.0.1", port=8010)
    assert seen["access_log"] is False


def test_errors_are_exact_and_do_not_reflect_token_question_or_internal_detail(
    tmp_path: Path,
) -> None:
    client = TestClient(_app(tmp_path))
    cases = (
        (
            _request(client, "POST", "/v1/questions", json_body={"question": "secret"}),
            401,
            "browser_session_unauthenticated",
        ),
        (
            _request(
                client,
                "POST",
                "/v1/questions",
                token="raw-secret-token",
                json_body={"question": "secret"},
            ),
            401,
            "browser_session_unauthenticated",
        ),
        (
            _request(
                client,
                "POST",
                "/v1/questions",
                token="valid-token",
                json_body={"question": "secret", "session": "caller-claim"},
            ),
            401,
            "browser_session_unauthenticated",
        ),
    )
    for response, status, code in cases:
        assert response.status_code == status
        assert response.json() == {"error": code}
        assert "secret" not in response.text
        assert "raw-secret-token" not in response.text

    _app_value, browser_client, headers = _browser_question_headers(tmp_path / "browser")
    http: Any = browser_client
    first = cast(Response, http.post("/v1/questions", headers=headers, json={"question": "first"}))
    duplicate = cast(Response, http.post("/v1/questions", headers=headers, json={"question": "second"}))
    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert duplicate.json() == {"error": "question_request_conflict"}


def test_empty_registry_keeps_readiness_and_question_session_unauthenticated(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path, bootstrap=False))
    assert _request(client, "GET", "/readyz").json() == {
        "error": "central_intake_unavailable"
    }
    response = _request(
        client,
        "POST",
        "/v1/questions",
        token="valid-token",
        json_body={"question": "must not persist"},
    )
    assert response.status_code == 401
    assert response.json() == {"error": "browser_session_unauthenticated"}


def test_browser_session_dependency_failure_is_question_lifecycle_unavailable(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, client, headers = _browser_question_headers(tmp_path)
    browser_auth = app.state.composition.browser_auth
    assert browser_auth is not None
    def unavailable_session(
        _session_digest: str, *, now: datetime
    ) -> tuple[BrowserSessionCurrentOutcome, None]:
        _ = now
        return BrowserSessionCurrentOutcome.UNAVAILABLE, None
    monkeypatch.setattr(browser_auth, "read_current_session", unavailable_session)
    http: Any = client
    response = cast(Response, http.post("/v1/questions", headers=headers, json={"question": "question"}))
    assert response.status_code == 503
    assert response.json() == {"error": "question_lifecycle_unavailable"}


def test_question_routes_reject_lone_utf8_surrogates_before_persisting(tmp_path: Path) -> None:
    app, client, headers = _browser_question_headers(tmp_path)
    http: Any = client
    malformed_question = cast(Response, http.post(
        "/v1/questions", headers=headers, content=b'{"question":"\\ud800"}',
    ))
    assert malformed_question.status_code == 422
    assert malformed_question.json() == {"error": "invalid_question_request"}
    assert app.state.composition.lifecycle_store is not None
    assert app.state.composition.lifecycle_store.get("request-1") is None

    policy_path = app.state.composition.config.authority_snapshot_path
    policy = cast(dict[str, object], yaml.safe_load(policy_path.read_text(encoding="utf-8")))
    permissions = cast(list[dict[str, object]], policy["role_permissions"])
    permissions[0]["actions"] = ["question.create", "question.read", "feedback.create"]
    policy["content_sha256"] = canonical_policy_digest(policy)
    policy_path.write_text(yaml.safe_dump(policy), encoding="utf-8")
    malformed_feedback = cast(Response, http.post(
        "/v1/questions/request-1/feedback", headers=headers,
        content=b'{"record_id":"record-1","verdict":"good","comment":"\\ud800"}',
    ))
    assert malformed_feedback.status_code == 422
    assert malformed_feedback.json() == {"error": "invalid_question_feedback"}


def test_question_routes_map_expired_ended_and_registry_drift_sessions_to_401(tmp_path: Path) -> None:
    for kind in ("expired", "ended", "registry-drift"):
        app, client, headers = _browser_question_headers(tmp_path / kind)
        session_handle = headers["cookie"].split(";", 1)[0].split("=", 1)[1]
        session_digest = sha256(session_handle.encode()).hexdigest()
        composition = app.state.composition
        assert composition.browser_auth is not None
        if kind == "ended":
            assert composition.browser_auth.end_session(session_digest, "logout", NOW)
        elif kind == "expired":
            # Advance the injected request clock instead of corrupting the
            # sealed Session row: corruption is intentionally dependency
            # unavailability (503), while ordinary expiry is unauthenticated.
            object.__setattr__(composition, "browser_clock", lambda: NOW + timedelta(hours=9))
        else:
            with sqlite3.connect(composition.config.database_path) as connection:
                connection.execute(
                    "UPDATE browser_sessions SET registry_revision=? WHERE session_digest=?",
                    (999, session_digest),
                )
        http: Any = client
        response = cast(Response, http.post("/v1/questions", headers=headers, json={"question": "question"}))
        assert response.status_code == 401
        assert response.json() == {"error": "browser_session_unauthenticated"}


def test_question_sse_rechecks_session_before_each_emitted_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client, headers = _browser_question_headers(tmp_path)
    http: Any = client
    created = cast(Response, http.post("/v1/questions", headers=headers, json={"question": "question"}))
    assert created.status_code == 201
    browser_auth = app.state.composition.browser_auth
    assert browser_auth is not None
    original_read = browser_auth.read_current_session
    reads = 0

    def revoked_after_route(
        session_digest: str, *, now: datetime,
    ) -> tuple[BrowserSessionCurrentOutcome, object | None]:
        nonlocal reads
        reads += 1
        if reads == 1:
            return original_read(session_digest, now=now)
        return BrowserSessionCurrentOutcome.UNAUTHENTICATED, None

    monkeypatch.setattr(browser_auth, "read_current_session", revoked_after_route)
    stream = cast(Response, http.get("/v1/questions/request-1/stream", headers={
        "cookie": headers["cookie"], "accept": "text/event-stream",
    }))
    assert stream.status_code == 200
    assert "event: accepted" not in stream.text
    assert "event: interrupted" in stream.text
    assert '"retryable":false' in stream.text


def test_tampered_persisted_received_envelope_never_returns_200(tmp_path: Path) -> None:
    app, client, headers = _browser_question_headers(tmp_path)
    http: Any = client
    created = cast(Response, http.post("/v1/questions", headers=headers, json={"question": "question"}))
    assert created.status_code == 201
    database = app.state.composition.config.database_path
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE question_requests SET revision=-1 WHERE request_id='request-1'"
        )
    response = cast(Response, http.get("/v1/questions/request-1", headers={"cookie": headers["cookie"]}))
    assert response.status_code == 503
    assert response.json() == {"error": "question_lifecycle_unavailable"}


def test_lifespan_and_runner_release_composition_owned_sqlite_handles(tmp_path: Path) -> None:
    app = _app(tmp_path)
    composition = app.state.composition
    with TestClient(app) as client:
        assert _request(client, "GET", "/healthz").status_code == 200
    assert composition.registry is not None
    assert composition.requests is not None
    assert composition.bootstrap_seals is not None
    with pytest.raises(sqlite3.ProgrammingError):
        composition.registry.users("acme")
    with pytest.raises(CentralQuestionRequestSqliteUnavailable):
        composition.requests.get("request-1")
    with pytest.raises(CentralBootstrapSealUnavailable):
        composition.bootstrap_seals.get("acme")
    composition.close()

    runner_app = _app(tmp_path / "runner")
    runner_composition = runner_app.state.composition

    def runner(app: FastAPI, *, host: str, port: int) -> None:
        _ = app, host, port

    run_central_api(runner_composition, runner)
    assert runner_composition.registry is not None
    with pytest.raises(sqlite3.ProgrammingError):
        runner_composition.registry.users("acme")


def test_private_inbox_routes_are_cookie_only_and_return_exact_empty_lists(
    tmp_path: Path,
) -> None:
    _app_value, client, headers = _browser_question_headers(tmp_path)
    http: Any = client
    cookie = {"cookie": headers["cookie"]}
    for path in (
        "/v1/inbox/conflicts",
        "/v1/inbox/backup-reviews",
        "/v1/inbox/reevaluations",
        "/v1/inbox/approvals",
    ):
        response = cast(Response, http.get(path, headers=cookie))
        assert response.status_code == 200, (path, response.text)
        assert response.json() == {"items": []}
        assert response.headers["cache-control"] == "no-store"
        assert cast(Response, http.get(path + "?page=1", headers=cookie)).json() == {
            "error": "invalid_input"
        }
        assert cast(
            Response,
            http.get(path, headers={**cookie, "x-aon-user": "forged"}),
        ).status_code == 422

    missing = cast(
        Response, http.get("/v1/inbox/conflicts/case-missing", headers=cookie)
    )
    assert missing.status_code == 404
    assert missing.json() == {"error": "not_found_or_denied"}
    assert cast(Response, http.get("/v1/inbox/conflicts")).json() == {
        "error": "session_unavailable"
    }


def test_private_inbox_writes_reject_unknown_dto_and_map_hidden_resource(
    tmp_path: Path,
) -> None:
    _app_value, client, headers = _browser_question_headers(tmp_path)
    http: Any = client
    post_headers = {**headers, "idempotency-key": "inbox-command-1"}
    cases = (
        (
            "/v1/inbox/conflicts/case-missing/concurrences",
            {
                "on_candidate_card_id": "card-1",
                "stance": "withdraw",
                "rationale": "not mine",
                "expected_case_revision": 1,
                "expected_request_revision": 1,
                "expected_round": 1,
            },
        ),
        (
            "/v1/inbox/backup-reviews/review-missing/dispositions",
            {"kind": "approve", "rationale": "ok", "expected_revision": 1},
        ),
        (
            "/v1/inbox/reevaluations/reeval-missing/dispositions",
            {
                "kind": "acknowledge",
                "rationale": "ok",
                "expected_revision": 1,
            },
        ),
        (
            "/v1/inbox/approvals/item-missing/dispositions",
            {
                "kind": "approve",
                "expected_approval_item_revision": 1,
                "expected_request_revision": 1,
            },
        ),
        (
            "/v1/inbox/approvals/item-missing/reassignments",
            {
                "target_approver_user_id": "root",
                "target_approval_card_id": "approval-card",
                "expected_approval_item_revision": 1,
                "expected_request_revision": 1,
            },
        ),
    )
    for path, body in cases:
        invalid = cast(
            Response,
            http.post(path, headers=post_headers, json={**body, "actor": "forged"}),
        )
        assert invalid.status_code == 422, (path, invalid.text)
        assert invalid.json() == {"error": "invalid_input"}
        hidden = cast(Response, http.post(path, headers=post_headers, json=body))
        assert hidden.status_code == 404, (path, hidden.text)
        assert hidden.json() == {"error": "not_found_or_denied"}

    bad_surrogate = cast(
        Response,
        http.post(
            "/v1/inbox/backup-reviews/review-missing/dispositions",
            headers=post_headers,
            content=(
                b'{"kind":"approve","rationale":"\\ud800","expected_revision":1}'
            ),
        ),
    )
    assert bad_surrogate.status_code == 422
    assert bad_surrogate.json() == {"error": "invalid_input"}


def test_private_inbox_reloads_policy_and_rechecks_session_inside_read_uow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client, headers = _browser_question_headers(tmp_path)
    http: Any = client
    cookie = {"cookie": headers["cookie"]}
    authority_path = app.state.composition.config.authority_snapshot_path
    policy = cast(dict[str, Any], yaml.safe_load(authority_path.read_text(encoding="utf-8")))
    permissions = cast(list[dict[str, Any]], policy["role_permissions"])
    admin = next(item for item in permissions if item["role"] == "admin")
    admin["actions"] = [
        action for action in cast(list[str], admin["actions"]) if action != "conflict.list"
    ]
    policy["content_sha256"] = "pending"
    policy["content_sha256"] = canonical_policy_digest(policy)
    authority_path.write_text(yaml.safe_dump(policy), encoding="utf-8")
    revoked = cast(Response, http.get("/v1/inbox/conflicts", headers=cookie))
    assert revoked.status_code == 404
    assert revoked.json() == {"error": "not_found_or_denied"}

    app2, client2, headers2 = _browser_question_headers(tmp_path / "session-race")
    browser_auth = app2.state.composition.browser_auth
    assert browser_auth is not None
    original = browser_auth.read_current_session

    def end_after_http_precheck(
        session_digest: str, *, now: datetime,
    ) -> tuple[BrowserSessionCurrentOutcome, object | None]:
        outcome, session = original(session_digest, now=now)
        if session is not None:
            with sqlite3.connect(app2.state.composition.config.database_path) as connection:
                connection.execute(
                    "UPDATE browser_sessions SET ended_at=?,terminal_reason='logout' "
                    "WHERE session_digest=? AND ended_at IS NULL",
                    (now.isoformat(), session_digest),
                )
        return outcome, session

    monkeypatch.setattr(browser_auth, "read_current_session", end_after_http_precheck)
    raced = cast(
        Response,
        cast(Any, client2).get(
            "/v1/inbox/conflicts", headers={"cookie": headers2["cookie"]}
        ),
    )
    assert raced.status_code == 401
    assert raced.json() == {"error": "session_unavailable"}


def test_private_approval_inbox_projects_same_snapshot_request_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client, headers = _browser_question_headers(tmp_path)
    assert isinstance(
        app.state.composition.approval_inbox, ApprovalInboxApplication
    )
    summary = ApprovalItemSummary(
        approval_item_id="approval-1",
        request_id="request-1",
        request_revision=3,
        approval_round=1,
        revision=1,
        assigned_at=NOW,
        due_at=NOW + timedelta(minutes=5),
        state="open",
    )
    detail = ApprovalItemDetail(
        approval_item_id=summary.approval_item_id,
        request_id=summary.request_id,
        request_revision=summary.request_revision,
        approval_round=summary.approval_round,
        revision=summary.revision,
        assigned_at=summary.assigned_at,
        due_at=summary.due_at,
        state=summary.state,
        question="approve?",
        candidate_text="candidate",
        candidate_digest="a" * 64,
        policy_digest="b" * 64,
        binding_version=1,
        assigned_approver_user_id="root",
        assigned_approval_card_id="approval-card",
    )
    def list_projection(
        _self: ApprovalInboxApplication, _command: ApprovalReadCommand
    ) -> tuple[ApprovalItemSummary, ...]:
        return (summary,)

    def detail_projection(
        _self: ApprovalInboxApplication,
        _command: ApprovalReadCommand,
        _approval_item_id: str,
    ) -> ApprovalItemDetail:
        return detail

    monkeypatch.setattr(ApprovalInboxApplication, "list", list_projection)
    monkeypatch.setattr(
        ApprovalInboxApplication,
        "detail",
        detail_projection,
    )

    http: Any = client
    cookie = {"cookie": headers["cookie"]}
    listed = cast(Response, http.get("/v1/inbox/approvals", headers=cookie))
    assert listed.status_code == 200
    assert listed.json() == {
        "items": [
            {
                "approval_item_id": "approval-1",
                "request_id": "request-1",
                "request_revision": 3,
                "approval_round": 1,
                "revision": 1,
                "assigned_at": NOW.isoformat().replace("+00:00", "Z"),
                "due_at": (NOW + timedelta(minutes=5))
                .isoformat()
                .replace("+00:00", "Z"),
                "state": "open",
            }
        ]
    }
    detailed = cast(
        Response, http.get("/v1/inbox/approvals/approval-1", headers=cookie)
    )
    assert detailed.status_code == 200
    assert detailed.json()["request_revision"] == 3
    assert set(detailed.json()) == {
        "approval_item_id",
        "request_id",
        "request_revision",
        "approval_round",
        "revision",
        "assigned_at",
        "due_at",
        "state",
        "question",
        "candidate_text",
        "candidate_digest",
        "policy_digest",
        "binding_version",
        "assigned_approver_user_id",
        "assigned_approval_card_id",
    }
