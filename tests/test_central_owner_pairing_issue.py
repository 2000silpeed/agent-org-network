# pyright: reportUnknownParameterType=false, reportMissingParameterType=false
# pyright: reportArgumentType=false, reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3
from threading import Barrier

from pydantic import SecretStr
import pytest
import yaml
from fastapi.testclient import TestClient

from agent_org_network.central_authority import (
    SnapshotCentralAuthorizer,
    canonical_policy_digest,
    load_authority_policy_yaml,
)
from agent_org_network.central_question_gateway import CentralQuestionGatewayRoutes
from agent_org_network.central_owner_pairing_issue import (
    CentralOwnerPairingIssueConflict,
    CentralOwnerPairingIssueStore,
    CentralOwnerPairingIssueUnavailable,
    CentralPairingServerKey,
    IssueOwnerPairingCommand,
    ProductionCentralPairingIssueAuthorizer,
    RedeemOwnerPairingCommand,
)
from agent_org_network.central_owner_pairing_client import RedeemedOwnerPairing
from agent_org_network.central_owner_pairing_web import (
    create_central_owner_pairing_app,
    create_production_central_authoring_pairing_app,
)
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
    SqliteProductionAuthoringRuns,
)
from agent_org_network.sqlite_production_registry_users import (
    CurrentUserRegistrationAuthorization,
    ProductionRegistryUserCommand,
    SqliteProductionRegistryUsers,
)
from agent_org_network.owner_credential_envelope import (
    decrypt_owner_credential,
    generate_device_keypair,
    serialize_owner_credential_envelope,
)


class _Keys:
    def current(self) -> CentralPairingServerKey:
        return CentralPairingServerKey(key_id="pair-key-1", key=b"k" * 32)


class _UserAllow:
    def current(self, command, transaction: sqlite3.Connection):
        return CurrentUserRegistrationAuthorization(
            authority_epoch=1, policy_digest="a" * 64, evidence_digest="b" * 64
        )

    def verify_precommit(self, command, evidence, transaction) -> bool:
        return True


class _CardAllow:
    def current(self, command, transaction: sqlite3.Connection):
        return CurrentCardRegistrationAuthorization(
            authority_epoch=1, policy_digest="a" * 64, evidence_digest="b" * 64
        )

    def verify_precommit(self, command, evidence, transaction) -> bool:
        return True


NOW = datetime(2026, 7, 28, tzinfo=UTC)


def _policy():
    document: dict[str, object] = {
        "schema_version": 1,
        "org_id": "acme",
        "policy_version": "policy-v1",
        "content_sha256": "pending",
        "subject_roles": [
            {"org_id": "acme", "subject_id": "owner", "roles": ["owner"]}
        ],
        "role_permissions": [{"role": "owner", "actions": ["author.write"]}],
        "route_rules": [],
        "worker_bindings": [],
    }
    document["content_sha256"] = canonical_policy_digest(document)
    return load_authority_policy_yaml(
        yaml.safe_dump(document, sort_keys=False), expected_org_id="acme"
    )


def _fixture(path: Path):
    SqliteProductionRegistryUsers.migrate(path)
    users = SqliteProductionRegistryUsers(path, authorize=_UserAllow())
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
    SqliteProductionAgentCards.migrate(path)
    cards = SqliteProductionAgentCards(path, authorize=_CardAllow())
    cards.register(
        ProductionAgentCardCommand(
            org_id="acme",
            principal_id="owner",
            idempotency_key="card-1",
            expected_revision=1,
            card={
                "agent_id": "support",
                "owner": "owner",
                "team": "support",
                "summary": "support",
                "domains": ["support"],
                "last_reviewed_at": "2026-07-28",
                "maintainer": None,
                "can_answer": [],
                "cannot_answer": [],
                "approval_when": [],
                "collaborate_when": [],
                "knowledge_sources": [],
                "trust_labels": [],
            },
        )
    )
    with sqlite3.connect(path) as connection:
        card_row = connection.execute(
            "SELECT revision,card_digest FROM production_agent_cards "
            "WHERE org_id='acme' AND agent_id='support'"
        ).fetchone()
    assert card_row is not None
    SqliteProductionIdentitySessions.migrate(path)
    sessions = SqliteProductionIdentitySessions(
        path,
        registry=users,
        configured_org_id="acme",
        provider_id="corp",
        issuer="https://id.example",
        clock=lambda: NOW,
        _identity_session_id_factory=lambda: "s" * 32,
    )
    sessions.establish(
        VerifiedEmailIdentityProof(
            provider_id="corp",
            issuer="https://id.example",
            email="owner@example.com",
            email_verified=True,
        ),
        expires_at=NOW + timedelta(hours=1),
    )
    snapshot = _policy()
    authorizer = ProductionCentralPairingIssueAuthorizer(
        policy_snapshot=lambda: snapshot,
        central_authorizer=SnapshotCentralAuthorizer(snapshot),
        identity_verifier=ProductionAuthoringIdentityVerifier(
            provider_id="corp",
            issuer="https://id.example",
            clock=lambda: NOW,
        ),
    )
    store = CentralOwnerPairingIssueStore(
        path, keys=_Keys(), authorizer=authorizer, clock=lambda: NOW
    )
    command = IssueOwnerPairingCommand(
        org_id="acme",
        principal_id="owner",
        agent_id="support",
        expected_card_revision=int(card_row[0]),
        expected_card_digest=str(card_row[1]),
        device_class="owner-desktop",
        idempotency_key="issue-1",
    )
    invocation = AuthoringInvocation(
        session=AuthoringIdentitySessionRef(value=SecretStr("s" * 32)),
        org_id="acme",
        principal_id="owner",
        identity_provider="corp",
    )
    return store, command, invocation


def test_issue는_same_tx_receipt_audit_outbox와_encrypted_replay다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "central.sqlite"
    store, command, invocation = _fixture(path)
    first = store.issue(command, invocation)
    replay = store.issue(command, invocation)
    assert replay.replayed
    assert replay.pairing_code.get_secret_value() == first.pairing_code.get_secret_value()
    assert "s" * 32 not in repr(first)
    disk = path.read_bytes()
    assert first.pairing_code.get_secret_value().encode() not in disk
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM central_owner_pairing_intents"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM central_owner_pairing_issue_receipts"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM central_owner_pairing_issue_audit"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM central_owner_pairing_issue_outbox"
        ).fetchone() == (1,)


def test_idempotency_conflict와_one_pending_card는deny다(tmp_path: Path) -> None:
    store, command, invocation = _fixture(tmp_path / "central.sqlite")
    store.issue(command, invocation)
    with pytest.raises(CentralOwnerPairingIssueConflict):
        store.issue(command.model_copy(update={"device_class": "other"}), invocation)
    with pytest.raises(CentralOwnerPairingIssueConflict):
        store.issue(command.model_copy(update={"idempotency_key": "issue-2"}), invocation)


def test_32way_issue는exact_one_intent_same_code다(tmp_path: Path) -> None:
    store, command, invocation = _fixture(tmp_path / "central.sqlite")
    barrier = Barrier(32)

    def issue(_index: int):
        barrier.wait()
        return store.issue(command, invocation)

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(issue, range(32)))
    assert len({item.intent_id for item in results}) == 1
    assert len({item.pairing_code.get_secret_value() for item in results}) == 1
    assert sum(not item.replayed for item in results) == 1


def test_companion_row_coordinated_tamper는_catalog복원뒤에도failclosed다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "central.sqlite"
    store, command, invocation = _fixture(path)
    store.issue(command, invocation)
    with sqlite3.connect(path) as connection:
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='trigger' "
            "AND name='central_owner_pairing_receipt_no_delete'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER central_owner_pairing_receipt_no_delete")
        connection.execute("DELETE FROM central_owner_pairing_issue_receipts")
        connection.execute(trigger_sql)
    with pytest.raises(CentralOwnerPairingIssueUnavailable):
        store.issue(command, invocation)


def test_verifier_only_coordinated_tamper는_decrypt후에도failclosed다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "central.sqlite"
    store, command, invocation = _fixture(path)
    store.issue(command, invocation)
    with sqlite3.connect(path) as connection:
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='trigger' "
            "AND name='central_owner_pairing_intent_exact_update'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER central_owner_pairing_intent_exact_update")
        connection.execute(
            "UPDATE central_owner_pairing_intents SET code_verifier=?",
            ("f" * 64,),
        )
        connection.execute(trigger_sql)
    with pytest.raises(CentralOwnerPairingIssueUnavailable):
        store.issue(command, invocation)


def test_issue_receipt_key_only_tamper는constructor와replay가failclosed다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "central.sqlite"
    store, command, invocation = _fixture(path)
    store.issue(command, invocation)
    with sqlite3.connect(path) as connection:
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='trigger' "
            "AND name='central_owner_pairing_receipt_immutable'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER central_owner_pairing_receipt_immutable")
        connection.execute(
            "UPDATE central_owner_pairing_issue_receipts "
            "SET idempotency_key='tampered-key'"
        )
        connection.execute(trigger_sql)
    with pytest.raises(CentralOwnerPairingIssueUnavailable):
        CentralOwnerPairingIssueStore(
            path,
            keys=_Keys(),
            authorizer=store._authorizer,  # pyright: ignore[reportPrivateUsage]
            clock=lambda: NOW,
        )
    with pytest.raises(CentralOwnerPairingIssueUnavailable):
        store.issue(command, invocation)


def test_idempotent_replay도_O1_current_session을다시검증한다(tmp_path: Path) -> None:
    path = tmp_path / "central.sqlite"
    store, command, invocation = _fixture(path)
    store.issue(command, invocation)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE production_identity_sessions SET active=0")
    with pytest.raises(CentralOwnerPairingIssueUnavailable):
        store.issue(command, invocation)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM central_owner_pairing_intents"
        ).fetchone() == (1,)


def test_expiry_equal_internal_clock인_replay는deny다(tmp_path: Path) -> None:
    store, command, invocation = _fixture(tmp_path / "central.sqlite")
    issued = store.issue(command, invocation)
    store._clock = lambda: issued.expires_at  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(CentralOwnerPairingIssueUnavailable):
        store.issue(command, invocation)


def test_first_generation_redeem과_exact_envelope_replay다(tmp_path: Path) -> None:
    path = tmp_path / "central.sqlite"
    store, issue_command, invocation = _fixture(path)
    issued = store.issue(issue_command, invocation)
    private, public = generate_device_keypair()
    command = RedeemOwnerPairingCommand(
        intent_id=issued.intent_id,
        pairing_code=issued.pairing_code,
        device_public_key=public,
        idempotency_key="redeem-1",
    )
    first = store.redeem(command)
    replay = store.redeem(command)
    assert issued.issue_receipt_id == issue_command.idempotency_key
    assert len(issued.issue_receipt_digest) == 64
    assert first.pairing_intent_digest == issued.pairing_intent_digest
    assert first.issue_receipt_digest == issued.issue_receipt_digest
    assert first.redeem_receipt_id == command.idempotency_key
    assert len(first.redeem_receipt_digest) == 64
    assert replay.replayed
    assert replay.credential_id == first.credential_id
    assert serialize_owner_credential_envelope(replay.envelope) == (
        serialize_owner_credential_envelope(first.envelope)
    )
    decrypted = decrypt_owner_credential(
        first.envelope, private, expected_aad=first.envelope.aad
    )
    assert (
        decrypted.credential_secret.get_secret_value().encode()
        not in path.read_bytes()
    )
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT generation,status FROM central_owner_pairing_credentials"
        ).fetchone() == (1, "active")
        assert connection.execute(
            "SELECT count(*) FROM central_owner_pairing_redeem_receipts"
        ).fetchone() == (1,)


def test_actual_http_issue_redeem과exact_replay다(tmp_path: Path) -> None:
    path = tmp_path / "central.sqlite"
    store, command, _invocation = _fixture(path)
    users = SqliteProductionRegistryUsers(path, authorize=_UserAllow())
    sessions = SqliteProductionIdentitySessions(
        path,
        registry=users,
        configured_org_id="acme",
        provider_id="corp",
        issuer="https://id.example",
        clock=lambda: NOW,
        _identity_session_id_factory=lambda: "x" * 32,
    )
    client = TestClient(
        create_central_owner_pairing_app(
            pairing=store,
            principal_resolver=ProductionPrincipalResolver(sessions),
        )
    )
    issue_body = {
        "agent_id": command.agent_id,
        "expected_card_revision": command.expected_card_revision,
        "expected_card_digest": command.expected_card_digest,
        "device_class": command.device_class,
    }
    headers = {
        "idempotency-key": command.idempotency_key,
        "cookie": f"aon_identity_session={'s' * 32}",
    }
    issued = client.post("/pairing/owner/issue", headers=headers, json=issue_body)
    issued_replay = client.post(
        "/pairing/owner/issue", headers=headers, json=issue_body
    )
    assert issued.status_code == issued_replay.status_code == 200
    assert issued.content == issued_replay.content
    assert issued.headers["cache-control"] == "no-store"
    _private, public = generate_device_keypair()
    redeem_body = {
        "intent_id": issued.json()["intent_id"],
        "pairing_code": issued.json()["pairing_code"],
        "device_public_key": public.model_dump(mode="json"),
    }
    redeem_headers = {"idempotency-key": "redeem-http-1"}
    redeemed = client.post(
        "/pairing/owner/redeem", headers=redeem_headers, json=redeem_body
    )
    redeemed_replay = client.post(
        "/pairing/owner/redeem", headers=redeem_headers, json=redeem_body
    )
    assert redeemed.status_code == redeemed_replay.status_code == 200
    assert redeemed.content == redeemed_replay.content
    assert redeemed.json()["envelope"]["aad"]["credential_id"] == (
        redeemed.json()["credential_id"]
    )
    assert redeemed.json()["device_key_thumbprint"] == (
        redeemed.json()["envelope"]["aad"]["device_key_thumbprint"]
    )
    parsed = RedeemedOwnerPairing.model_validate_json(redeemed.content)
    assert parsed.credential_id == redeemed.json()["credential_id"]
    assert redeemed.json()["issue_receipt_id"] == issued.json()["issue_receipt_id"]
    assert len(redeemed.json()["redeem_receipt_digest"]) == 64
    assert issued.json()["pairing_code"].encode() not in path.read_bytes()


def test_actual_http_redeem은revoked_session과cross_device를deny한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "central.sqlite"
    store, command, invocation = _fixture(path)
    issued = store.issue(command, invocation)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE production_identity_sessions SET active=0")
    users = SqliteProductionRegistryUsers(path, authorize=_UserAllow())
    sessions = SqliteProductionIdentitySessions(
        path,
        registry=users,
        configured_org_id="acme",
        provider_id="corp",
        issuer="https://id.example",
        clock=lambda: NOW,
        _identity_session_id_factory=lambda: "x" * 32,
    )
    client = TestClient(
        create_central_owner_pairing_app(
            pairing=store,
            principal_resolver=ProductionPrincipalResolver(sessions),
        )
    )
    _private, public = generate_device_keypair()
    body = {
        "intent_id": issued.intent_id,
        "pairing_code": issued.pairing_code.get_secret_value(),
        "device_public_key": public.model_dump(mode="json"),
    }
    denied = client.post(
        "/pairing/owner/redeem",
        headers={"idempotency-key": "redeem-http-1"},
        json=body,
    )
    assert denied.status_code == 503

    other_path = tmp_path / "cross.sqlite"
    other, other_command, other_invocation = _fixture(other_path)
    other_issued = other.issue(other_command, other_invocation)
    other_users = SqliteProductionRegistryUsers(other_path, authorize=_UserAllow())
    other_sessions = SqliteProductionIdentitySessions(
        other_path,
        registry=other_users,
        configured_org_id="acme",
        provider_id="corp",
        issuer="https://id.example",
        clock=lambda: NOW,
        _identity_session_id_factory=lambda: "x" * 32,
    )
    other_client = TestClient(
        create_central_owner_pairing_app(
            pairing=other,
            principal_resolver=ProductionPrincipalResolver(other_sessions),
        )
    )
    _first_private, first_public = generate_device_keypair()
    first = other_client.post(
        "/pairing/owner/redeem",
        headers={"idempotency-key": "redeem-cross"},
        json={
            "intent_id": other_issued.intent_id,
            "pairing_code": other_issued.pairing_code.get_secret_value(),
            "device_public_key": first_public.model_dump(mode="json"),
        },
    )
    assert first.status_code == 200
    _second_private, second_public = generate_device_keypair()
    cross = other_client.post(
        "/pairing/owner/redeem",
        headers={"idempotency-key": "redeem-cross"},
        json={
            "intent_id": other_issued.intent_id,
            "pairing_code": other_issued.pairing_code.get_secret_value(),
            "device_public_key": second_public.model_dump(mode="json"),
        },
    )
    assert cross.status_code == 409


@pytest.mark.parametrize(
    ("headers", "content"),
    [
        ({"content-type": "text/plain", "content-length": "2"}, b"{}"),
        ({"content-type": "application/json", "content-length": ""}, b"{}"),
        ({"content-type": "application/json", "content-length": "100"}, b"{}"),
        ({"content-type": "application/json", "content-length": "20000"}, b"{}"),
        (
            {"content-type": "application/json", "content-length": "2"},
            b"{" + b"x" * 17000 + b"}",
        ),
    ],
)
def test_pairing_http_malformed_framing은422_no_store다(
    tmp_path: Path, headers: dict[str, str], content: bytes
) -> None:
    path = tmp_path / "central.sqlite"
    store, _command, _invocation = _fixture(path)
    users = SqliteProductionRegistryUsers(path, authorize=_UserAllow())
    sessions = SqliteProductionIdentitySessions(
        path,
        registry=users,
        configured_org_id="acme",
        provider_id="corp",
        issuer="https://id.example",
        clock=lambda: NOW,
        _identity_session_id_factory=lambda: "x" * 32,
    )
    client = TestClient(
        create_central_owner_pairing_app(
            pairing=store,
            principal_resolver=ProductionPrincipalResolver(sessions),
        )
    )
    response = client.post(
        "/pairing/owner/redeem",
        headers={**headers, "idempotency-key": "redeem-invalid"},
        content=content,
    )
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert "pairing_code" not in response.text


@pytest.mark.parametrize(
    ("idempotency_key", "body"),
    [
        ("bad key", {}),
        ("redeem-invalid", {}),
        (
            "redeem-invalid",
            {
                "intent_id": "intent-1",
                "pairing_code": "short",
                "device_public_key": {"kty": "OKP", "crv": "X25519", "x": "bad"},
            },
        ),
    ],
)
def test_pairing_http_invalid_header_code_jwk는secretless_422다(
    tmp_path: Path, idempotency_key: str, body: object
) -> None:
    path = tmp_path / "central.sqlite"
    store, _command, _invocation = _fixture(path)
    users = SqliteProductionRegistryUsers(path, authorize=_UserAllow())
    sessions = SqliteProductionIdentitySessions(
        path,
        registry=users,
        configured_org_id="acme",
        provider_id="corp",
        issuer="https://id.example",
        clock=lambda: NOW,
        _identity_session_id_factory=lambda: "x" * 32,
    )
    client = TestClient(
        create_central_owner_pairing_app(
            pairing=store,
            principal_resolver=ProductionPrincipalResolver(sessions),
        )
    )
    response = client.post(
        "/pairing/owner/redeem",
        headers={"idempotency-key": idempotency_key},
        json=body,
    )
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert "short" not in response.text
    assert "session" not in response.text.lower()
    assert "credential" not in response.text.lower()


def test_production_combined_composition은exact_instances와routes만받는다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "central.sqlite"
    pairing, _command, _invocation = _fixture(path)
    users = SqliteProductionRegistryUsers(path, authorize=_UserAllow())
    sessions = SqliteProductionIdentitySessions(
        path,
        registry=users,
        configured_org_id="acme",
        provider_id="corp",
        issuer="https://id.example",
        clock=lambda: NOW,
        _identity_session_id_factory=lambda: "x" * 32,
    )
    snapshot = _policy()
    authoring_authorizer = ProductionCentralTxCurrentAuthoringAuthorizer(
        policy_snapshot=lambda: snapshot,
        central_authorizer=SnapshotCentralAuthorizer(snapshot),
        identity_verifier=ProductionAuthoringIdentityVerifier(
            provider_id="corp",
            issuer="https://id.example",
            clock=lambda: NOW,
        ),
    )
    SqliteProductionAuthoringRuns.migrate(path)
    runs = SqliteProductionAuthoringRuns(path, authorize=authoring_authorizer)
    class _QuestionApplication:
        def ask(self, command: object) -> str:
            del command
            return "asked"

        def lookup(self, request_id: str, principal: object) -> str:
            del request_id, principal
            return "looked-up"

    class _Oidc:
        def verify(self, token: str) -> object:
            del token
            return object()

    class _Principals:
        def resolve(self, claims: object) -> object:
            del claims
            return object()

    class _Owners:
        def resolve_question_owner(self, request_id: str) -> None:
            del request_id
            return None

    class _Authority:
        def authorize(self, *args: object) -> object:
            del args
            return object()

        def verify(self, *args: object) -> bool:
            del args
            return False

    question_gateway = CentralQuestionGatewayRoutes(
        application=_QuestionApplication(), oidc=_Oidc(), principals=_Principals(),
        request_owners=_Owners(), authority=_Authority(), render=str,
    )
    app = create_production_central_authoring_pairing_app(
        runs=runs,
        authoring_authorizer=authoring_authorizer,
        pairing=pairing,
        pairing_authorizer=pairing._authorizer,  # pyright: ignore[reportPrivateUsage]
        principal_resolver=ProductionPrincipalResolver(sessions),
        question_gateway=question_gateway,
    )
    paths = {
        path
        for route in app.routes
        if isinstance(path := getattr(route, "path", None), str)
    }
    assert {
        "/authoring/runs/start",
        "/authoring/runs/complete",
        "/pairing/owner/issue",
        "/pairing/owner/redeem",
        "/ask_org",
        "/get_question",
    }.issubset(paths)
    assert not any("publish" in path for path in paths)
    with pytest.raises(CentralOwnerPairingIssueUnavailable):
        create_production_central_authoring_pairing_app(
            runs=runs,
            authoring_authorizer=authoring_authorizer,
            pairing=pairing,
            pairing_authorizer=object(),
            principal_resolver=ProductionPrincipalResolver(sessions),
        )


def test_redeemed_code의_different_device_or_idempotency는conflict다(
    tmp_path: Path,
) -> None:
    store, issue_command, invocation = _fixture(tmp_path / "central.sqlite")
    issued = store.issue(issue_command, invocation)
    _private, public = generate_device_keypair()
    command = RedeemOwnerPairingCommand(
        intent_id=issued.intent_id,
        pairing_code=issued.pairing_code,
        device_public_key=public,
        idempotency_key="redeem-1",
    )
    store.redeem(command)
    _other_private, other_public = generate_device_keypair()
    with pytest.raises(CentralOwnerPairingIssueConflict):
        store.redeem(
            command.model_copy(update={"device_public_key": other_public})
        )
    with pytest.raises(CentralOwnerPairingIssueConflict):
        store.redeem(
            command.model_copy(update={"idempotency_key": "redeem-2"})
        )


def test_32way_redeem은one_credential_byte_identical_replay다(
    tmp_path: Path,
) -> None:
    store, issue_command, invocation = _fixture(tmp_path / "central.sqlite")
    issued = store.issue(issue_command, invocation)
    _private, public = generate_device_keypair()
    command = RedeemOwnerPairingCommand(
        intent_id=issued.intent_id,
        pairing_code=issued.pairing_code,
        device_public_key=public,
        idempotency_key="redeem-1",
    )
    barrier = Barrier(32)

    def redeem(_index: int):
        barrier.wait()
        return store.redeem(command)

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(redeem, range(32)))
    assert len({result.credential_id for result in results}) == 1
    assert len(
        {
            serialize_owner_credential_envelope(result.envelope)
            for result in results
        }
    ) == 1
    assert sum(not result.replayed for result in results) == 1


@pytest.mark.parametrize(
    "point",
    [
        "before_credential",
        "after_credential",
        "before_redeem_precommit",
        "before_redeem_commit",
    ],
)
def test_redeem_fault는_all_or_nothing이다(
    tmp_path: Path, point: str
) -> None:
    path = tmp_path / "central.sqlite"
    store, issue_command, invocation = _fixture(path)
    issued = store.issue(issue_command, invocation)
    _private, public = generate_device_keypair()

    def fault(current: str) -> None:
        if current == point:
            raise RuntimeError("credential-secret-must-not-leak")

    crashing = CentralOwnerPairingIssueStore(
        path,
        keys=_Keys(),
        authorizer=store._authorizer,  # pyright: ignore[reportPrivateUsage]
        clock=lambda: NOW,
        fault=fault,
    )
    with pytest.raises(CentralOwnerPairingIssueUnavailable) as raised:
        crashing.redeem(
            RedeemOwnerPairingCommand(
                intent_id=issued.intent_id,
                pairing_code=issued.pairing_code,
                device_public_key=public,
                idempotency_key="redeem-1",
            )
        )
    assert "credential-secret" not in str(raised.value)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT state FROM central_owner_pairing_intents"
        ).fetchone() == ("pending",)
        assert connection.execute(
            "SELECT count(*) FROM central_owner_pairing_credentials"
        ).fetchone() == (0,)


def test_session_revoke와_replay_window_expiry는redelivery0_active보존이다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "central.sqlite"
    store, issue_command, invocation = _fixture(path)
    issued = store.issue(issue_command, invocation)
    _private, public = generate_device_keypair()
    command = RedeemOwnerPairingCommand(
        intent_id=issued.intent_id,
        pairing_code=issued.pairing_code,
        device_public_key=public,
        idempotency_key="redeem-1",
    )
    store.redeem(command)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE production_identity_sessions SET active=0")
    with pytest.raises(CentralOwnerPairingIssueUnavailable):
        store.redeem(command)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT status FROM central_owner_pairing_credentials"
        ).fetchone() == ("active",)

    second_path = tmp_path / "window.sqlite"
    second, second_issue, second_invocation = _fixture(second_path)
    second_issued = second.issue(second_issue, second_invocation)
    _private2, public2 = generate_device_keypair()
    second_command = RedeemOwnerPairingCommand(
        intent_id=second_issued.intent_id,
        pairing_code=second_issued.pairing_code,
        device_public_key=public2,
        idempotency_key="redeem-2",
    )
    second.redeem(second_command)
    second._clock = lambda: NOW + timedelta(minutes=2)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(CentralOwnerPairingIssueUnavailable):
        second.redeem(second_command)
    with sqlite3.connect(second_path) as connection:
        assert connection.execute(
            "SELECT status FROM central_owner_pairing_credentials"
        ).fetchone() == ("active",)


def _redeemed_fixture(path: Path):
    store, issue_command, invocation = _fixture(path)
    issued = store.issue(issue_command, invocation)
    _private, public = generate_device_keypair()
    command = RedeemOwnerPairingCommand(
        intent_id=issued.intent_id,
        pairing_code=issued.pairing_code,
        device_public_key=public,
        idempotency_key="redeem-retention",
    )
    store.redeem(command)
    return store, command


def test_replay_retention은expiry_equal에ciphertext만purge한다(tmp_path: Path) -> None:
    path = tmp_path / "central.sqlite"
    store, command = _redeemed_fixture(path)
    with sqlite3.connect(path) as connection:
        before = {
            table: connection.execute(f"SELECT * FROM {table}").fetchall()
            for table in (
                "central_owner_pairing_credentials",
                "central_owner_pairing_redeem_receipts",
                "central_owner_pairing_redeem_audit",
                "central_owner_pairing_redeem_outbox",
            )
        }
    assert store.purge_expired_replay_envelopes() == 0
    store._clock = lambda: NOW + timedelta(minutes=2)  # pyright: ignore[reportPrivateUsage]
    assert store.purge_expired_replay_envelopes() == 1
    assert store.purge_expired_replay_envelopes() == 0
    with pytest.raises(CentralOwnerPairingIssueUnavailable):
        store.redeem(command)
    with sqlite3.connect(path) as connection:
        replay = connection.execute(
            "SELECT envelope_json,replay_expires_at,purged_at FROM "
            "central_owner_pairing_replay_envelopes"
        ).fetchone()
        assert replay == (
            None,
            "2026-07-28T00:02:00Z",
            "2026-07-28T00:02:00Z",
        )
        assert (
            connection.execute(
                "SELECT count(envelope_json) FROM "
                "central_owner_pairing_replay_envelopes"
            ).fetchone()
            == (0,)
        )
        for table, rows in before.items():
            assert connection.execute(f"SELECT * FROM {table}").fetchall() == rows


def test_replay_retention_direct_early_update는trigger가막는다(tmp_path: Path) -> None:
    path = tmp_path / "central.sqlite"
    _redeemed_fixture(path)
    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE central_owner_pairing_replay_envelopes "
                "SET envelope_json=NULL,purged_at=?",
                ("2026-07-28T00:01:59Z",),
            )


@pytest.mark.parametrize(
    "point", ["after_replay_purge", "before_replay_purge_commit"]
)
def test_replay_retention_fault는ciphertext를rollback한다(
    tmp_path: Path, point: str
) -> None:
    path = tmp_path / "central.sqlite"
    store, _command = _redeemed_fixture(path)

    def fault(current: str) -> None:
        if current == point:
            raise RuntimeError("purge fault")

    crashing = CentralOwnerPairingIssueStore(
        path,
        keys=_Keys(),
        authorizer=store._authorizer,  # pyright: ignore[reportPrivateUsage]
        clock=lambda: NOW + timedelta(minutes=2),
        fault=fault,
    )
    with pytest.raises(CentralOwnerPairingIssueUnavailable):
        crashing.purge_expired_replay_envelopes()
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT envelope_json IS NOT NULL,purged_at FROM "
            "central_owner_pairing_replay_envelopes"
        ).fetchone() == (1, None)


def test_replay_retention_동시purge는exactly_once다(tmp_path: Path) -> None:
    path = tmp_path / "central.sqlite"
    store, _command = _redeemed_fixture(path)
    store._clock = lambda: NOW + timedelta(minutes=2)  # pyright: ignore[reportPrivateUsage]
    barrier = Barrier(16)

    def purge(_index: int) -> int:
        barrier.wait()
        return store.purge_expired_replay_envelopes()

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(purge, range(16)))
    assert results.count(1) == 1
    assert results.count(0) == 15


def test_redeem_companion_coordinated_tamper는failclosed다(tmp_path: Path) -> None:
    path = tmp_path / "central.sqlite"
    store, issue_command, invocation = _fixture(path)
    issued = store.issue(issue_command, invocation)
    _private, public = generate_device_keypair()
    command = RedeemOwnerPairingCommand(
        intent_id=issued.intent_id,
        pairing_code=issued.pairing_code,
        device_public_key=public,
        idempotency_key="redeem-1",
    )
    store.redeem(command)
    with sqlite3.connect(path) as connection:
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='trigger' "
            "AND name='central_owner_pairing_redeem_audit_no_delete'"
        ).fetchone()[0]
        connection.execute(
            "DROP TRIGGER central_owner_pairing_redeem_audit_no_delete"
        )
        connection.execute("DELETE FROM central_owner_pairing_redeem_audit")
        connection.execute(trigger_sql)
    with pytest.raises(CentralOwnerPairingIssueUnavailable):
        store.redeem(command)


def test_redeem_receipt_key_only_tamper는constructor와replay가failclosed다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "central.sqlite"
    store, issue_command, invocation = _fixture(path)
    issued = store.issue(issue_command, invocation)
    _private, public = generate_device_keypair()
    command = RedeemOwnerPairingCommand(
        intent_id=issued.intent_id,
        pairing_code=issued.pairing_code,
        device_public_key=public,
        idempotency_key="redeem-1",
    )
    store.redeem(command)
    with sqlite3.connect(path) as connection:
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='trigger' "
            "AND name='central_owner_pairing_redeem_receipt_immutable'"
        ).fetchone()[0]
        connection.execute(
            "DROP TRIGGER central_owner_pairing_redeem_receipt_immutable"
        )
        connection.execute(
            "UPDATE central_owner_pairing_redeem_receipts "
            "SET idempotency_key='tampered-key'"
        )
        connection.execute(trigger_sql)
    with pytest.raises(CentralOwnerPairingIssueUnavailable):
        CentralOwnerPairingIssueStore(
            path,
            keys=_Keys(),
            authorizer=store._authorizer,  # pyright: ignore[reportPrivateUsage]
            clock=lambda: NOW,
        )
    with pytest.raises(CentralOwnerPairingIssueUnavailable):
        store.redeem(command)


def test_credential_verifier_coordinated_tamper는_audit_anchor가거부한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "central.sqlite"
    store, issue_command, invocation = _fixture(path)
    issued = store.issue(issue_command, invocation)
    _private, public = generate_device_keypair()
    command = RedeemOwnerPairingCommand(
        intent_id=issued.intent_id,
        pairing_code=issued.pairing_code,
        device_public_key=public,
        idempotency_key="redeem-1",
    )
    store.redeem(command)
    with sqlite3.connect(path) as connection:
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='trigger' "
            "AND name='central_owner_pairing_credential_immutable'"
        ).fetchone()[0]
        connection.execute(
            "DROP TRIGGER central_owner_pairing_credential_immutable"
        )
        connection.execute(
            "UPDATE central_owner_pairing_credentials SET secret_verifier=?",
            ("f" * 64,),
        )
        connection.execute(trigger_sql)
    with pytest.raises(CentralOwnerPairingIssueUnavailable):
        store.redeem(command)


@pytest.mark.parametrize("point", ["before_intent", "before_precommit", "before_commit"])
def test_fault는_all_or_nothing이다(tmp_path: Path, point: str) -> None:
    path = tmp_path / "central.sqlite"
    store, command, invocation = _fixture(path)

    def fault(current: str) -> None:
        if current == point:
            raise RuntimeError("raw-secret-must-not-leak")

    crashing = CentralOwnerPairingIssueStore(
        path,
        keys=_Keys(),
        authorizer=store._authorizer,  # pyright: ignore[reportPrivateUsage]
        clock=lambda: NOW,
        fault=fault,
    )
    with pytest.raises(CentralOwnerPairingIssueUnavailable) as raised:
        crashing.issue(command, invocation)
    assert "raw-secret" not in str(raised.value)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM central_owner_pairing_intents"
        ).fetchone() == (0,)
