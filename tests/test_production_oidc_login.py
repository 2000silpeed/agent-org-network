from datetime import UTC, datetime, timedelta
import base64
import json
from pathlib import Path
from typing import Any, cast
import urllib.parse

from fastapi.testclient import TestClient
from httpx import Response
import pytest

from agent_org_network.oidc import FakeOidcProvider, OidcClaims
from agent_org_network.production_identity_sessions import (
    ProductionPrincipalResolver,
    SqliteProductionIdentitySessions,
)
from agent_org_network.production_oidc_login import (
    HttpAuthorizationCodeOidcProvider,
    OidcLoginTransactions,
    ProductionOidcLoginUnavailable,
    authorization_url,
)
from agent_org_network.production_onboarding_web import create_production_onboarding_app
from agent_org_network.sqlite_production_registry_users import (
    CurrentUserRegistrationAuthorization,
    ProductionRegistryUserCommand,
    SqliteProductionRegistryUsers,
)


class _Auth:
    def current(self, command: object, transaction: object) -> CurrentUserRegistrationAuthorization:
        _ = command, transaction
        return CurrentUserRegistrationAuthorization(
            authority_epoch=1, policy_digest="b" * 64, evidence_digest="a" * 64
        )

    def verify_precommit(
        self, command: object, evidence: object, transaction: object
    ) -> bool:
        _ = command, evidence, transaction
        return True


def _jwt(nonce: str) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"nonce": nonce}).encode()
    ).rstrip(b"=").decode()
    return f"header.{payload}.signature"


def _app(tmp_path: Path, provider: object, transactions: OidcLoginTransactions) -> TestClient:
    registry_path = tmp_path / "registry.db"
    SqliteProductionRegistryUsers.migrate(registry_path)
    users = SqliteProductionRegistryUsers(registry_path, authorize=cast(Any, _Auth()))
    users.register(
        ProductionRegistryUserCommand(
            org_id="acme", principal_id="bootstrap", idempotency_key="bootstrap-1",
            expected_revision=0, user_id="root", email="root@company.com",
        )
    )
    identity_path = tmp_path / "identity.db"
    SqliteProductionIdentitySessions.migrate(identity_path)
    sessions = SqliteProductionIdentitySessions(
        identity_path, registry=users, configured_org_id="acme",
        provider_id="corp", issuer="https://id.test",
        _identity_session_id_factory=lambda: "s" * 32,
    )
    return TestClient(
        create_production_onboarding_app(
            users=users,
            principal_resolver=ProductionPrincipalResolver(sessions),
            oidc_provider=cast(Any, provider),
            oidc_transactions=transactions,
            oidc_authorization_url="https://id.test/authorize",
            oidc_client_id="client",
            oidc_redirect_uri="https://central.test/auth/oidc/callback",
        ),
        base_url="https://central.test",
    )


class _CodeProvider:
    provider_id = "corp"
    issuer = "https://id.test"

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def exchange(self, **values: str) -> Any:
        self.calls.append(values)
        from agent_org_network.production_identity_sessions import VerifiedEmailIdentityProof
        return VerifiedEmailIdentityProof(
            provider_id=self.provider_id, issuer=self.issuer,
            email="root@company.com", email_verified=True,
        )


def test_start와_callback은_pkce_nonce_opaque_session만_cookie에_둔다(tmp_path: Path) -> None:
    tokens = iter(("x" * 32, "y" * 32, "n" * 32, "v" * 32))
    transactions = OidcLoginTransactions(random_token=lambda: next(tokens))
    provider = _CodeProvider()
    client = _app(tmp_path, provider, transactions)

    start = cast(Response, cast(Any, client).get("/auth/oidc/start", follow_redirects=False))
    assert start.status_code == 302
    query = urllib.parse.parse_qs(urllib.parse.urlparse(start.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["nonce"] == ["n" * 32]
    assert "HttpOnly" in start.headers["set-cookie"]
    assert "Secure" in start.headers["set-cookie"]
    assert "id_token" not in start.headers["set-cookie"]

    callback = cast(Response, cast(Any, client).get(
        "/auth/oidc/callback", params={"state": "x" * 32, "code": "secret-code"}
    ))
    assert callback.status_code == 200
    assert provider.calls[0]["code_verifier"] == "v" * 32
    cookie = callback.headers["set-cookie"]
    assert "aon_identity_session=" + "s" * 32 in cookie
    assert "aon_oidc_transaction=" in cookie and "Max-Age=0" in cookie
    assert "secret-code" not in cookie
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie


def test_state는_cross_browser_expiry_replay를_거부한다(tmp_path: Path) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    clock_value = [now]
    tokens = iter(("a" * 32, "b" * 32, "c" * 32, "d" * 32))
    transactions = OidcLoginTransactions(
        clock=lambda: clock_value[0], ttl=timedelta(seconds=1),
        random_token=lambda: next(tokens),
    )
    provider = _CodeProvider()
    client = _app(tmp_path, provider, transactions)
    cast(Any, client).get("/auth/oidc/start", follow_redirects=False)
    cast(Any, client).cookies.set("aon_oidc_transaction", "z" * 32)
    denied = cast(Response, cast(Any, client).get(
        "/auth/oidc/callback", params={"state": "a" * 32, "code": "code"}
    ))
    assert denied.status_code == 401
    assert "aon_oidc_transaction=" in denied.headers["set-cookie"]
    assert "Max-Age=0" in denied.headers["set-cookie"]
    assert cast(Any, client).get(
        "/auth/oidc/callback", params={"state": "a" * 32, "code": "code"}
    ).status_code == 401


def test_provider는_verified_token_nonce와_pkce_form을강제한다() -> None:
    token = _jwt("nonce")
    forms: list[dict[str, list[str]]] = []
    provider = HttpAuthorizationCodeOidcProvider(
        provider_id="corp", issuer="https://id.test", client_id="client",
        client_secret="secret", token_url="https://id.test/token",
        id_token_verifier=FakeOidcProvider(tokens={
            token: OidcClaims(
                sub="subject", email="root@company.com", email_verified=True,
                iss="https://id.test", aud="client",
            )
        }),
        post_form=lambda _url, body: (
            forms.append(urllib.parse.parse_qs(body.decode())) or {"id_token": token}
        ),
    )
    proof = provider.exchange(
        code="code", redirect_uri="https://central.test/callback",
        code_verifier="verifier", expected_nonce="nonce",
    )
    assert proof.email == "root@company.com"
    assert forms[0]["code_verifier"] == ["verifier"]
    with pytest.raises(ProductionOidcLoginUnavailable):
        provider.exchange(
            code="code", redirect_uri="https://central.test/callback",
            code_verifier="wrong", expected_nonce="other",
        )


def test_idp_error는내용을반사하지않는다(tmp_path: Path) -> None:
    client = _app(tmp_path, _CodeProvider(), OidcLoginTransactions())
    response = cast(Response, cast(Any, client).get(
        "/auth/oidc/callback",
        params={"error": "<script>secret</script>", "error_description": "token-secret"},
    ))
    assert response.status_code == 401
    assert "script" not in response.text
    assert "token-secret" not in response.text
    assert "Max-Age=0" in response.headers["set-cookie"]


def test_idp_error도_valid_transaction을소진해후속성공replay를막는다(
    tmp_path: Path,
) -> None:
    tokens = iter(("q" * 32, "w" * 32, "e" * 32, "r" * 32))
    client = _app(
        tmp_path, _CodeProvider(), OidcLoginTransactions(random_token=lambda: next(tokens))
    )
    cast(Any, client).get("/auth/oidc/start", follow_redirects=False)
    failed = cast(Response, cast(Any, client).get(
        "/auth/oidc/callback",
        params={"state": "q" * 32, "error": "access_denied"},
    ))
    assert failed.status_code == 401
    assert "Max-Age=0" in failed.headers["set-cookie"]
    cast(Any, client).cookies.set("aon_oidc_transaction", "w" * 32)
    replay = cast(Response, cast(Any, client).get(
        "/auth/oidc/callback", params={"state": "q" * 32, "code": "later-code"}
    ))
    assert replay.status_code == 401
    assert replay.json() == {"detail": "SSO authentication failed"}


def test_transaction_repr은nonce와verifier를숨긴다() -> None:
    tokens = iter(("1" * 32, "2" * 32, "3" * 32, "4" * 32))
    transaction = OidcLoginTransactions(random_token=lambda: next(tokens)).begin()
    rendered = repr(transaction)
    assert "2" * 32 not in rendered
    assert "3" * 32 not in rendered
    assert "4" * 32 not in rendered
    assert "browser_binding=" not in rendered
    assert "nonce=" not in rendered
    assert "code_verifier=" not in rendered


@pytest.mark.parametrize(
    "url",
    [
        "http://id.test/path",
        "https:///missing-host",
        "https://user:pass@id.test/path",
        "https://id.test/path#fragment",
    ],
)
def test_provider는악성endpoint_config를거부한다(url: str) -> None:
    with pytest.raises(ProductionOidcLoginUnavailable):
        HttpAuthorizationCodeOidcProvider(
            provider_id="corp", issuer="https://id.test", client_id="client",
            client_secret=None, token_url=url, id_token_verifier=FakeOidcProvider(),
        )


def test_authorize_url은query와안전하지않은redirect를거부한다() -> None:
    tokens = iter(("a" * 32, "b" * 32, "c" * 32, "d" * 32))
    transaction = OidcLoginTransactions(random_token=lambda: next(tokens)).begin()
    for endpoint, redirect in (
        ("https://id.test/authorize?prompt=login", "https://central.test/callback"),
        ("https://id.test/authorize", "http://central.test/callback"),
        ("https://id.test/authorize", "https://user@central.test/callback"),
    ):
        with pytest.raises(ProductionOidcLoginUnavailable):
            authorization_url(
                endpoint=endpoint, client_id="client", redirect_uri=redirect,
                transaction=transaction,
            )
