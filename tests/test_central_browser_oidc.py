"""RB3.2b.2-C deterministic Central browser code-flow contracts."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from email.message import Message
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from urllib.error import HTTPError
from urllib.parse import parse_qs

import pytest

import agent_org_network.central_browser_oidc as browser_oidc_module

from agent_org_network.central_authority import (
    BrowserSessionAuthorityAllowed,
    BrowserSessionAuthorityDenied,
    BrowserSessionAuthorityResult,
    BrowserSessionAuthorityUnavailable,
)
from agent_org_network.central_browser_auth import BrowserPkceVerifierVault
from agent_org_network.central_browser_auth_sqlite import (
    CentralBrowserAuthSqliteStore,
    migrate_browser_auth_schema,
)
from agent_org_network.central_browser_oidc import (
    BROWSER_OIDC_TRANSACTION_TTL,
    BROWSER_SESSION_TTL,
    BrowserOidcCallbackInvalid,
    BrowserOidcForbidden,
    BrowserOidcNotAdmitted,
    BrowserOidcSessionApplication,
    BrowserSessionForbidden,
    BrowserSessionUnauthenticated,
    BrowserOidcUnauthenticated,
    BrowserOidcUnavailable,
    FakeOidcAuthorizationCodeExchange,
    HttpOidcAuthorizationCodeExchange,
)
from agent_org_network.oidc import FakeOidcProvider, OidcClaims
from agent_org_network.sqlite_production_registry_users import (
    CurrentUserRegistrationAuthorization,
    ProductionRegistryUserCommand,
    SqliteProductionRegistryUsers,
)


NOW = datetime(2026, 7, 31, 8, 0, tzinfo=UTC)


class _RegistrationAuthorizer:
    def current(
        self, command: ProductionRegistryUserCommand, transaction: sqlite3.Connection
    ) -> CurrentUserRegistrationAuthorization:
        _ = command, transaction
        return CurrentUserRegistrationAuthorization(
            authority_epoch=1, policy_digest="a" * 64, evidence_digest="b" * 64
        )

    def verify_precommit(
        self,
        command: ProductionRegistryUserCommand,
        evidence: CurrentUserRegistrationAuthorization,
        transaction: sqlite3.Connection,
    ) -> bool:
        _ = command, evidence, transaction
        return True


class _Authority:
    def __init__(self, outcomes: list[BrowserSessionAuthorityResult] | None = None) -> None:
        self._outcomes = outcomes or [BrowserSessionAuthorityAllowed()]
        self.calls = 0

    def authorize(self, *_args: object, **_kwargs: object) -> BrowserSessionAuthorityResult:
        result = self._outcomes[min(self.calls, len(self._outcomes) - 1)]
        self.calls += 1
        return result


def _application(
    tmp_path: Path,
    *,
    clock: list[datetime] | None = None,
    exchange: FakeOidcAuthorizationCodeExchange | None = None,
    authority: _Authority | None = None,
    vault: BrowserPkceVerifierVault | None = None,
) -> tuple[BrowserOidcSessionApplication, CentralBrowserAuthSqliteStore, SqliteProductionRegistryUsers]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "central.sqlite3"
    SqliteProductionRegistryUsers.migrate_v2(path)
    migrate_browser_auth_schema(path)
    registry = SqliteProductionRegistryUsers(path, authorize=_RegistrationAuthorizer())
    registry.register(
        ProductionRegistryUserCommand(
            org_id="acme", principal_id="root", idempotency_key="root-1", expected_revision=0,
            user_id="root", email="root@example.test"
        )
    )
    store = CentralBrowserAuthSqliteStore(path)
    current = clock or [NOW]
    fake = exchange or FakeOidcAuthorizationCodeExchange(
        {"code-ok": ("https://idp.example.test", "browser-client", "root@example.test", "subject-1", "nonce-00000000000000000000000000000001")},
        issuer="https://idp.example.test", audience="browser-client",
    )
    values = iter((
        "transaction-handle-000000000000000001", "state-value-00000000000000000000001",
        "nonce-00000000000000000000000000000001", "verifier-000000000000000000000000001", "session-handle-0000000000000000000001",
        "csrf-token-000000000000000000000000001",
    ))
    app = BrowserOidcSessionApplication(
        org_id="acme", provider_id="company-oidc", issuer="https://idp.example.test",
        authorization_url="https://idp.example.test/authorize", client_id="browser-client",
        scope="openid email", redirect_uri="https://central.example.test/api/auth/callback",
        transactions=store, registry=registry, authority=authority or _Authority(), exchange=fake,
        vault=vault or BrowserPkceVerifierVault(clock=lambda: current[0]), clock=lambda: current[0],
        random_handle=lambda: next(values),
    )
    return app, store, registry


def test_begin_and_callback_use_fixed_pkce_redirect_and_establish_one_digest_session(tmp_path: Path) -> None:
    app, store, registry = _application(tmp_path)
    try:
        start = app.begin()
        assert "code_challenge_method=S256" in start.authorization_url
        assert "redirect_uri=https%3A%2F%2Fcentral.example.test%2Fapi%2Fauth%2Fcallback" in start.authorization_url
        complete = app.complete(
            transaction_handle=start.transaction_handle, state="state-value-00000000000000000000001", authorization_code="code-ok"
        )
        assert complete.session_handle not in repr(complete)
        assert store.get_session(sha256(complete.session_handle.encode()).hexdigest()) is not None
        with pytest.raises(BrowserOidcCallbackInvalid):
            app.complete(
                transaction_handle=start.transaction_handle, state="state-value-00000000000000000000001", authorization_code="code-ok"
            )
    finally:
        store.close()
        registry.close()

    app, store, registry = _application(
        tmp_path / "revoke",
        authority=_Authority([BrowserSessionAuthorityAllowed(), BrowserSessionAuthorityDenied()]),
    )
    try:
        start = app.begin()
        with pytest.raises(BrowserOidcForbidden):
            app.complete(
                transaction_handle=start.transaction_handle,
                state="state-value-00000000000000000000001",
                authorization_code="code-ok",
            )
        assert store.get_session(sha256("session-handle-0000000000000000000001".encode()).hexdigest()) is None
    finally:
        store.close()
        registry.close()

@pytest.mark.parametrize("state", ["wrong-state", "state-value-00000000000000000000001"])
def test_callback_state_and_vault_loss_fail_without_session(tmp_path: Path, state: str) -> None:
    app, store, registry = _application(tmp_path)
    try:
        start = app.begin()
        if state == "state-value-00000000000000000000001":
            app._vault.clear()  # pyright: ignore[reportPrivateUsage] - restart model
            expected: type[Exception] = BrowserOidcUnavailable
        else:
            expected = BrowserOidcCallbackInvalid
        with pytest.raises(expected):
            app.complete(transaction_handle=start.transaction_handle, state=state, authorization_code="code-ok")
        assert store.get_session(sha256("session-handle-0000000000000000000001".encode()).hexdigest()) is None
    finally:
        store.close()
        registry.close()


def test_standard_idp_error_cancels_transaction_and_ttl_bounds_are_strict(tmp_path: Path) -> None:
    app, store, registry = _application(tmp_path)
    try:
        start = app.begin()
        app.cancel(transaction_handle=start.transaction_handle, state="state-value-00000000000000000000001")
        transaction = store.get_transaction(sha256(start.transaction_handle.encode()).hexdigest())
        assert transaction is not None and transaction.terminal_reason == "cancelled"
        with pytest.raises(BrowserOidcCallbackInvalid):
            app.complete(transaction_handle=start.transaction_handle, state="state-value-00000000000000000000001", authorization_code="code-ok")
        with pytest.raises(ValueError):
            BrowserOidcSessionApplication(
                org_id="acme", provider_id="company", issuer="https://idp.example.test",
                authorization_url="https://idp.example.test/authorize", client_id="browser-client",
                scope="openid email", redirect_uri="https://central.example.test/api/auth/callback",
                transactions=store, registry=registry, authority=_Authority(),
                exchange=FakeOidcAuthorizationCodeExchange(issuer="https://idp.example.test", audience="browser-client"),
                vault=BrowserPkceVerifierVault(), transaction_ttl=BROWSER_OIDC_TRANSACTION_TTL + timedelta(seconds=1),
            )
        assert BROWSER_SESSION_TTL == timedelta(hours=8)
    finally:
        store.close()
        registry.close()


def test_unknown_identity_and_current_authority_drift_create_no_session(tmp_path: Path) -> None:
    exchange = FakeOidcAuthorizationCodeExchange(
        {"unknown": ("https://idp.example.test", "browser-client", "unknown@example.test", "subject", "nonce-00000000000000000000000000000001")},
        issuer="https://idp.example.test", audience="browser-client",
    )
    app, store, registry = _application(tmp_path, exchange=exchange)
    try:
        start = app.begin()
        with pytest.raises(BrowserOidcNotAdmitted):
            app.complete(transaction_handle=start.transaction_handle, state="state-value-00000000000000000000001", authorization_code="unknown")
        assert store.get_session(sha256("session-handle-0000000000000000000001".encode()).hexdigest()) is None
    finally:
        store.close()
        registry.close()


def test_current_session_rechecks_registry_expiry_and_authority_on_every_read(tmp_path: Path) -> None:
    current = [NOW]
    authority = _Authority([
        BrowserSessionAuthorityAllowed(), BrowserSessionAuthorityAllowed(),
        BrowserSessionAuthorityDenied(), BrowserSessionAuthorityUnavailable(),
    ])
    app, store, registry = _application(tmp_path, clock=current, authority=authority)
    try:
        start = app.begin()
        complete = app.complete(
            transaction_handle=start.transaction_handle,
            state="state-value-00000000000000000000001",
            authorization_code="code-ok",
        )
        with pytest.raises(BrowserSessionForbidden):
            app.read(session_handle=complete.session_handle)
        with pytest.raises(BrowserOidcUnavailable):
            app.read(session_handle=complete.session_handle)
        current[0] = NOW + BROWSER_SESSION_TTL
        with pytest.raises(BrowserSessionUnauthenticated):
            app.read(session_handle=complete.session_handle)
    finally:
        store.close()
        registry.close()


def test_current_session_rejects_registry_revision_or_fingerprint_drift(tmp_path: Path) -> None:
    app, store, registry = _application(tmp_path)
    try:
        start = app.begin()
        complete = app.complete(
            transaction_handle=start.transaction_handle,
            state="state-value-00000000000000000000001",
            authorization_code="code-ok",
        )
        digest = sha256(complete.session_handle.encode()).hexdigest()
        with sqlite3.connect(tmp_path / "central.sqlite3") as connection:
            connection.execute("UPDATE browser_sessions SET registry_revision=999 WHERE session_digest=?", (digest,))
        with pytest.raises(BrowserSessionUnauthenticated):
            app.read(session_handle=complete.session_handle)
    finally:
        store.close()
        registry.close()


def test_http_public_code_exchange_has_no_secret_and_redacts_token_endpoint_failures() -> None:
    nonce = "nonce-00000000000000000000000000000001"
    payload = base64.urlsafe_b64encode(json.dumps({"nonce": nonce}).encode()).rstrip(b"=").decode()
    token = "x." + payload + ".x"
    observed: list[dict[str, list[str]]] = []

    def post(_url: str, body: bytes) -> dict[str, object]:
        observed.append(parse_qs(body.decode("ascii")))
        return {"id_token": token}

    exchange = HttpOidcAuthorizationCodeExchange(
        token_url="https://idp.example.test/token", client_id="browser-client",
        issuer="https://idp.example.test",
        id_token_verifier=FakeOidcProvider({token: OidcClaims(
            sub="subject", email="root@example.test", email_verified=True,
            iss="https://idp.example.test", aud="browser-client",
        )}),
        post_form=post,
    )
    identity = exchange.exchange(
        authorization_code="raw-code", code_verifier="raw-verifier",
        redirect_uri="https://central.example.test/api/auth/callback",
        expected_nonce_digest=sha256(nonce.encode()).hexdigest(),
    )
    assert identity.identity_binding_digest
    assert set(observed[0]) == {"grant_type", "code", "redirect_uri", "client_id", "code_verifier"}
    assert "client_secret" not in observed[0]

    for status, expected in ((400, BrowserOidcUnauthenticated), (401, BrowserOidcUnauthenticated), (500, BrowserOidcUnavailable)):
        def rejected(_url: str, _body: bytes, *, status_code: int = status) -> dict[str, object]:
            raise HTTPError("https://idp.example.test/token", status_code, "raw error", Message(), None)

        rejected_exchange = HttpOidcAuthorizationCodeExchange(
            token_url="https://idp.example.test/token", client_id="browser-client",
            issuer="https://idp.example.test", id_token_verifier=FakeOidcProvider(), post_form=rejected,
        )
        with pytest.raises(expected) as error:
            rejected_exchange.exchange(
                authorization_code="raw-code", code_verifier="raw-verifier",
                redirect_uri="https://central.example.test/api/auth/callback",
                expected_nonce_digest=sha256(nonce.encode()).hexdigest(),
            )
        assert "raw error" not in str(error.value)


def test_default_token_post_never_follows_307_with_code_or_verifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[bytes] = []

    class RedirectingOpener:
        def __init__(self, handlers: tuple[object, ...]) -> None:
            self._handlers = handlers

        def open(self, request: object, *, timeout: int) -> object:
            _ = timeout
            body = getattr(request, "data")
            assert type(body) is bytes
            seen.append(body)
            for handler in self._handlers:
                redirect = getattr(handler, "redirect_request", None)
                if callable(redirect) and redirect(None, None, 307, "temporary", {}, "https://evil.test"):
                    seen.append(b"second-target-request")
            raise HTTPError("https://idp.example.test/token", 307, "redirect", Message(), None)

    def opener_factory(*handlers: object) -> RedirectingOpener:
        return RedirectingOpener(handlers)

    monkeypatch.setattr(browser_oidc_module, "build_opener", opener_factory)
    with pytest.raises(BrowserOidcUnavailable) as error:
        browser_oidc_module._post_form(  # pyright: ignore[reportPrivateUsage]
            "https://idp.example.test/token", b"code=raw-code&code_verifier=raw-verifier"
        )
    assert len(seen) == 1
    assert b"raw-code" not in str(error.value).encode()
    assert b"raw-verifier" not in str(error.value).encode()
