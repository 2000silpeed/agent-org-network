"""Central-only browser authorization-code + PKCE application boundary.

The module intentionally does not reuse the legacy production login flow.  Its
durable inputs are digests only; the few opaque browser values below are held
only long enough for the HTTP adapter to put them in a protected cookie or an
IdP redirect query.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import hmac
import json
from typing import Any, Protocol, cast
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    BrowserSessionAuthority,
    BrowserSessionAuthorityAllowed,
    BrowserSessionAuthorityUnavailable,
    ResourceRef,
)
from agent_org_network.central_browser_auth import (
    BrowserOidcTransaction,
    BrowserPkceVerifierVault,
    BrowserSession,
    constant_time_digest_matches,
    new_opaque_browser_handle,
    opaque_browser_handle_digest,
)
from agent_org_network.central_browser_auth_sqlite import (
    BrowserSessionCurrentOutcome,
    BrowserSessionEstablishmentOutcome,
    CentralBrowserAuthSqliteStore,
)
from agent_org_network.oidc import OidcClaims, OidcProvider, OidcVerificationError
from agent_org_network.sqlite_production_registry_users import (
    ProductionRegistryUser,
    SqliteProductionRegistryUsers,
    production_registry_user_fingerprint,
)


class BrowserOidcCallbackInvalid(RuntimeError):
    """Callback correlation, replay, or expiry failed without detail."""


class BrowserOidcUnauthenticated(RuntimeError):
    """The IdP could not prove the configured browser identity."""


class BrowserOidcNotAdmitted(RuntimeError):
    """The verified identity is not an existing Registry User."""


class BrowserOidcForbidden(RuntimeError):
    """Current Central Authority denied session establishment."""


class BrowserOidcUnavailable(RuntimeError):
    """A dependency or bounded verifier vault is unavailable."""


class BrowserSessionUnauthenticated(RuntimeError):
    """The opaque browser session is absent, inactive, or no longer current."""


class BrowserSessionForbidden(RuntimeError):
    """The current Central Authority denied a session read."""


class BrowserSessionCsrfForbidden(RuntimeError):
    """Logout proof is missing or invalid without exposing session existence."""


class _TokenEndpointRejected(RuntimeError):
    """A redacted token endpoint 4xx such as invalid_grant."""


BROWSER_OIDC_TRANSACTION_TTL = timedelta(minutes=5)
BROWSER_SESSION_TTL = timedelta(hours=8)


@dataclass(frozen=True, slots=True)
class VerifiedBrowserOidcIdentity:
    """Process-local verified proof; raw IdP claims never appear in repr/JSON."""

    issuer: str
    audience: str
    identity_binding_digest: str
    _email: str = field(repr=False, compare=False)

    def existing_registry_user(
        self, registry: SqliteProductionRegistryUsers, *, org_id: str
    ) -> ProductionRegistryUser:
        try:
            user = registry.user_by_global_email(self._email)
        except Exception as error:
            raise BrowserOidcUnavailable() from error
        if type(user) is not ProductionRegistryUser or user.org_id != org_id:
            raise BrowserOidcNotAdmitted()
        return user


class OidcAuthorizationCodeExchangePort(Protocol):
    """The only port permitted to receive an authorization code/verifier."""

    def exchange(
        self,
        *,
        authorization_code: str,
        code_verifier: str,
        redirect_uri: str,
        expected_nonce_digest: str,
    ) -> VerifiedBrowserOidcIdentity: ...


class FakeOidcAuthorizationCodeExchange:
    """Deterministic code-exchange double; test values never leave the adapter."""

    def __init__(
        self,
        codes: dict[str, tuple[str, str, str, str, str]] | None = None,
        *,
        issuer: str,
        audience: str,
    ) -> None:
        self._codes = dict(codes or {})
        self._issuer = issuer
        self._audience = audience
        self.calls: list[tuple[str, str]] = []

    def exchange(
        self,
        *,
        authorization_code: str,
        code_verifier: str,
        redirect_uri: str,
        expected_nonce_digest: str,
    ) -> VerifiedBrowserOidcIdentity:
        self.calls.append((_digest(code_verifier), redirect_uri))
        item = self._codes.get(authorization_code)
        if item is None:
            raise BrowserOidcUnauthenticated()
        issuer, audience, email, subject, nonce = item
        if (
            issuer != self._issuer
            or audience != self._audience
            or not constant_time_digest_matches(nonce, expected_nonce_digest)
        ):
            raise BrowserOidcUnauthenticated()
        return _identity(issuer=issuer, audience=audience, email=email, subject=subject)


class HttpOidcAuthorizationCodeExchange:
    """Public-client token exchange and verified nonce proof, with no secret."""

    def __init__(
        self,
        *,
        token_url: str,
        client_id: str,
        issuer: str,
        id_token_verifier: OidcProvider,
        post_form: Callable[[str, bytes], dict[str, Any]] | None = None,
    ) -> None:
        if not all((token_url, client_id, issuer)) or not _https_url(token_url) or not _https_url(issuer):
            raise ValueError("browser OIDC public-client configuration required")
        self._token_url = token_url
        self._client_id = client_id
        self._issuer = issuer
        self._verifier = id_token_verifier
        self._post_form = post_form or _post_form

    def exchange(
        self,
        *,
        authorization_code: str,
        code_verifier: str,
        redirect_uri: str,
        expected_nonce_digest: str,
    ) -> VerifiedBrowserOidcIdentity:
        if not all((authorization_code, code_verifier, redirect_uri, expected_nonce_digest)):
            raise BrowserOidcUnauthenticated()
        try:
            response = self._post_form(
                self._token_url,
                urlencode(
                    {
                        "grant_type": "authorization_code",
                        "code": authorization_code,
                        "redirect_uri": redirect_uri,
                        "client_id": self._client_id,
                        "code_verifier": code_verifier,
                    }
                ).encode("ascii"),
            )
            token = response.get("id_token")
            if type(token) is not str:
                raise BrowserOidcUnauthenticated()
            claims = self._verifier.verify(token)
            nonce = _verified_nonce(token)
            if (
                type(claims) is not OidcClaims
                or claims.iss != self._issuer
                or claims.aud != self._client_id
                or not claims.email_verified
                or not constant_time_digest_matches(nonce, expected_nonce_digest)
            ):
                raise BrowserOidcUnauthenticated()
            return _identity(
                issuer=claims.iss, audience=claims.aud, email=claims.email, subject=claims.sub
            )
        except BrowserOidcUnauthenticated:
            raise
        except _TokenEndpointRejected:
            raise BrowserOidcUnauthenticated() from None
        except HTTPError as error:
            if 400 <= error.code < 500:
                raise BrowserOidcUnauthenticated() from None
            raise BrowserOidcUnavailable() from None
        except OidcVerificationError:
            raise BrowserOidcUnauthenticated() from None
        except Exception:
            raise BrowserOidcUnavailable() from None


@dataclass(frozen=True, slots=True)
class BrowserOidcStartWire:
    """HTTP-only transaction cookie material; its repr deliberately redacts it."""

    authorization_url: str
    transaction_handle: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class BrowserOidcCompleteWire:
    """HTTP-only successful session cookie material; never serialize this DTO."""

    session_handle: str = field(repr=False)
    csrf_token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class BrowserSessionProjection:
    """Safe session-read projection; browser wire material never appears here."""

    authenticated: bool
    registry_user_ref: str
    expires_at: datetime
    actions: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.authenticated is not True or not self.registry_user_ref or self.actions != ("session.read",):
            raise ValueError("safe browser session projection required")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("safe browser session expiry required")


class BrowserSessionApplication:
    """Current/end boundary independent from the IdP code-exchange capability."""

    def __init__(
        self,
        *,
        provider_id: str,
        transactions: CentralBrowserAuthSqliteStore,
        authority: BrowserSessionAuthority,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not provider_id:
            raise ValueError("strict browser session application configuration required")
        self._provider_id = provider_id
        self._transactions = transactions
        self._authority = authority
        self._clock = clock

    def read(self, *, session_handle: str) -> BrowserSessionProjection:
        """Resolve one opaque handle through current Registry and Authority state."""
        try:
            now = self._now()
            session_digest = opaque_browser_handle_digest(session_handle)
            outcome, session = self._transactions.read_current_session(session_digest, now=now)
            if outcome is BrowserSessionCurrentOutcome.UNAUTHENTICATED:
                raise BrowserSessionUnauthenticated()
            if outcome is not BrowserSessionCurrentOutcome.ACTIVE or session is None:
                raise BrowserOidcUnavailable()
            principal = AuthenticatedPrincipal(
                org_id=session.org_id,
                subject_id=session.registry_user_id,
                identity_provider=self._provider_id,
                identity_session_id=session.session_digest,
            )
            resource = ResourceRef(
                org_id=session.org_id, kind="browser_session", resource_id=session.session_digest
            )
            authorized = self._authority.authorize(principal, "session.read", resource)
            if type(authorized) is BrowserSessionAuthorityUnavailable:
                raise BrowserOidcUnavailable()
            if type(authorized) is not BrowserSessionAuthorityAllowed:
                raise BrowserSessionForbidden()
            return BrowserSessionProjection(
                authenticated=True,
                registry_user_ref=session.registry_user_id,
                expires_at=session.expires_at,
                actions=("session.read",),
            )
        except (BrowserSessionUnauthenticated, BrowserSessionForbidden, BrowserOidcUnavailable):
            raise
        except Exception:
            raise BrowserOidcUnavailable() from None

    def end(self, *, session_handle: str, csrf_cookie: str, csrf_header: str) -> None:
        """End only the proved local session; policy/Registry are intentionally irrelevant."""
        try:
            if (
                type(session_handle) is not str
                or type(csrf_cookie) is not str
                or type(csrf_header) is not str
                or not csrf_cookie
                or not csrf_header
                or not hmac.compare_digest(csrf_cookie, csrf_header)
            ):
                raise BrowserSessionCsrfForbidden()
            now = self._now()
            session_digest = opaque_browser_handle_digest(session_handle)
            session = self._transactions.get_session(session_digest)
            if session is None or not constant_time_digest_matches(csrf_cookie, session.csrf_digest):
                raise BrowserSessionCsrfForbidden()
            self._transactions.end_session(session_digest, "logout", now)
        except BrowserSessionCsrfForbidden:
            raise
        except Exception:
            raise BrowserOidcUnavailable() from None

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise BrowserOidcUnavailable()
        return now


class BrowserOidcSessionApplication(BrowserSessionApplication):
    """Begin and complete one Central browser OIDC transaction."""

    def __init__(
        self,
        *,
        org_id: str,
        provider_id: str,
        issuer: str,
        authorization_url: str,
        client_id: str,
        scope: str,
        redirect_uri: str,
        transactions: CentralBrowserAuthSqliteStore,
        registry: SqliteProductionRegistryUsers,
        authority: BrowserSessionAuthority,
        exchange: OidcAuthorizationCodeExchangePort,
        vault: BrowserPkceVerifierVault,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        random_handle: Callable[[], str] = new_opaque_browser_handle,
        transaction_ttl: timedelta = BROWSER_OIDC_TRANSACTION_TTL,
        session_ttl: timedelta = BROWSER_SESSION_TTL,
    ) -> None:
        if (
            not all((org_id, provider_id, issuer, authorization_url, client_id, scope, redirect_uri))
            or scope != "openid email"
            or transaction_ttl <= timedelta() or transaction_ttl > BROWSER_OIDC_TRANSACTION_TTL
            or session_ttl <= timedelta() or session_ttl > BROWSER_SESSION_TTL
            or transaction_ttl > vault.ttl
        ):
            raise ValueError("strict browser OIDC application configuration required")
        super().__init__(
            provider_id=provider_id,
            transactions=transactions,
            authority=authority,
            clock=clock,
        )
        self._org_id = org_id
        self._issuer = issuer
        self._authorization_url = authorization_url
        self._client_id = client_id
        self._scope = scope
        self._redirect_uri = redirect_uri
        self._registry = registry
        self._exchange = exchange
        self._vault = vault
        self._random = random_handle
        self._transaction_ttl = transaction_ttl
        self._session_ttl = session_ttl

    def begin(self) -> BrowserOidcStartWire:
        try:
            now = self._now()
            transaction_handle, state, nonce, verifier = self._new_distinct_values(4)
            transaction_digest = opaque_browser_handle_digest(transaction_handle)
            expires_at = now + self._transaction_ttl
            redirect = _authorization_redirect(
                endpoint=self._authorization_url,
                client_id=self._client_id,
                redirect_uri=self._redirect_uri,
                scope=self._scope,
                state=state,
                nonce=nonce,
                verifier=verifier,
            )
            if not self._vault.reserve(transaction_digest, verifier, expires_at):
                raise BrowserOidcUnavailable()
            try:
                self._transactions.create_transaction(
                    BrowserOidcTransaction(
                        transaction_digest=transaction_digest,
                        provider_digest=_digest(self._issuer),
                        redirect_uri_digest=_digest(self._redirect_uri),
                        state_digest=_digest(state),
                        nonce_digest=_digest(nonce),
                        created_at=now,
                        expires_at=expires_at,
                    )
                )
            except Exception:
                self._vault.discard(transaction_digest)
                raise BrowserOidcUnavailable() from None
            return BrowserOidcStartWire(
                authorization_url=redirect,
                transaction_handle=transaction_handle,
            )
        except BrowserOidcUnavailable:
            raise
        except Exception:
            raise BrowserOidcUnavailable() from None

    def complete(
        self, *, transaction_handle: str, state: str, authorization_code: str
    ) -> BrowserOidcCompleteWire:
        try:
            now = self._now()
            transaction_digest = opaque_browser_handle_digest(transaction_handle)
            transaction = self._transactions.get_transaction(transaction_digest)
            if (
                type(transaction) is not BrowserOidcTransaction
                or transaction.consumed_at is not None
                or transaction.expires_at <= now
                or transaction.provider_digest != _digest(self._issuer)
                or transaction.redirect_uri_digest != _digest(self._redirect_uri)
                or not constant_time_digest_matches(state, transaction.state_digest)
            ):
                raise BrowserOidcCallbackInvalid()
            verifier = self._vault.consume(transaction_digest)
            if verifier is None:
                raise BrowserOidcUnavailable()
            identity = self._exchange.exchange(
                authorization_code=authorization_code,
                code_verifier=verifier,
                redirect_uri=self._redirect_uri,
                expected_nonce_digest=transaction.nonce_digest,
            )
            if type(identity) is not VerifiedBrowserOidcIdentity:
                raise BrowserOidcUnavailable()
            user = identity.existing_registry_user(self._registry, org_id=self._org_id)
            session_handle, csrf_token = self._new_distinct_values(2)
            session = BrowserSession(
                session_digest=opaque_browser_handle_digest(session_handle),
                registry_user_id=user.user_id,
                org_id=user.org_id,
                oidc_identity_binding_digest=identity.identity_binding_digest,
                csrf_digest=opaque_browser_handle_digest(csrf_token),
                registry_fingerprint=production_registry_user_fingerprint(
                    user.org_id, user.user_id, user.email, user.manager_id, user.revision
                ),
                registry_revision=user.revision,
                established_at=now,
                expires_at=now + self._session_ttl,
            )
            principal = AuthenticatedPrincipal(
                org_id=user.org_id,
                subject_id=user.user_id,
                identity_provider=self._provider_id,
                identity_session_id=session.session_digest,
            )
            resource = ResourceRef(
                org_id=user.org_id, kind="browser_session", resource_id=session.session_digest
            )
            first = self._authority.authorize(principal, "session.establish", resource)
            if type(first) is BrowserSessionAuthorityUnavailable:
                raise BrowserOidcUnavailable()
            if type(first) is not BrowserSessionAuthorityAllowed:
                raise BrowserOidcForbidden()

            def precommit(_connection: object) -> bool:
                current = self._authority.authorize(principal, "session.establish", resource)
                if type(current) is BrowserSessionAuthorityUnavailable:
                    raise BrowserOidcUnavailable()
                return type(current) is BrowserSessionAuthorityAllowed

            outcome = self._transactions.establish_session(
                transaction_digest, session, now=now, precommit_authorize=precommit
            )
            if outcome is BrowserSessionEstablishmentOutcome.ESTABLISHED:
                return BrowserOidcCompleteWire(session_handle=session_handle, csrf_token=csrf_token)
            if outcome is BrowserSessionEstablishmentOutcome.DENIED:
                raise BrowserOidcForbidden()
            if outcome is BrowserSessionEstablishmentOutcome.INVALID:
                raise BrowserOidcCallbackInvalid()
            raise BrowserOidcUnavailable()
        except (
            BrowserOidcCallbackInvalid,
            BrowserOidcUnauthenticated,
            BrowserOidcNotAdmitted,
            BrowserOidcForbidden,
            BrowserOidcUnavailable,
        ):
            raise
        except Exception:
            raise BrowserOidcUnavailable() from None

    def cancel(self, *, transaction_handle: str, state: str) -> None:
        """Consume a verified IdP error callback so it can never become code flow."""
        try:
            now = self._now()
            digest = opaque_browser_handle_digest(transaction_handle)
            transaction = self._transactions.get_transaction(digest)
            if (
                type(transaction) is not BrowserOidcTransaction
                or transaction.consumed_at is not None
                or transaction.expires_at <= now
                or not constant_time_digest_matches(state, transaction.state_digest)
            ):
                raise BrowserOidcCallbackInvalid()
            self._vault.discard(digest)
            if not self._transactions.mark_transaction_consumed(digest, "cancelled", now):
                raise BrowserOidcCallbackInvalid()
        except BrowserOidcCallbackInvalid:
            raise
        except Exception:
            raise BrowserOidcUnavailable() from None

    def _new_distinct_values(self, count: int) -> tuple[str, ...]:
        values = tuple(self._random() for _ in range(count))
        if len(set(values)) != count or any(len(value) < 32 for value in values):
            raise BrowserOidcUnavailable()
        return values


def _identity(*, issuer: str, audience: str, email: str, subject: str) -> VerifiedBrowserOidcIdentity:
    if not all((issuer, audience, email, subject)):
        raise BrowserOidcUnauthenticated()
    return VerifiedBrowserOidcIdentity(
        issuer=issuer,
        audience=audience,
        identity_binding_digest=_digest(issuer + "\x00" + subject + "\x00" + audience),
        _email=email,
    )


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _authorization_redirect(
    *, endpoint: str, client_id: str, redirect_uri: str, scope: str, state: str, nonce: str, verifier: str
) -> str:
    challenge = base64.urlsafe_b64encode(sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    separator = "&" if "?" in endpoint else "?"
    return endpoint + separator + urlencode(
        {
            "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
            "scope": scope, "state": state, "nonce": nonce, "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )


def _post_form(url: str, body: bytes) -> dict[str, Any]:
    try:
        request = Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        )
        opener = build_opener(_NoRedirectHandler())
        with opener.open(request, timeout=10) as response:  # noqa: S310 - strict trusted profile URL
            if response.headers.get_content_type() != "application/json":
                raise BrowserOidcUnavailable()
            raw = response.read(64 * 1024 + 1)
            if len(raw) > 64 * 1024:
                raise BrowserOidcUnavailable()
            parsed: object = json.loads(raw)
    except HTTPError as error:
        if 400 <= error.code < 500:
            raise _TokenEndpointRejected() from None
        raise BrowserOidcUnavailable() from None
    except Exception:
        raise BrowserOidcUnavailable() from None
    if not isinstance(parsed, dict):
        raise BrowserOidcUnavailable()
    return cast(dict[str, Any], parsed)


def _https_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


class _NoRedirectHandler(HTTPRedirectHandler):
    """Never replay a code/verifier-bearing POST to a redirect target."""

    def redirect_request(self, *args: object, **kwargs: object) -> Request | None:
        _ = args, kwargs
        return None


def _verified_nonce(id_token: str) -> str:
    try:
        parts = id_token.split(".")
        if len(parts) != 3:
            raise ValueError
        payload: object = json.loads(
            base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        )
        values = cast(dict[object, object], payload) if isinstance(payload, dict) else {}
        nonce: object = values.get("nonce")
        if type(nonce) is not str or not nonce:
            raise ValueError
        return nonce
    except Exception:
        raise BrowserOidcUnauthenticated() from None


__all__ = [
    "BrowserOidcCallbackInvalid", "BrowserOidcCompleteWire", "BrowserOidcForbidden",
    "BrowserOidcNotAdmitted", "BrowserOidcSessionApplication", "BrowserOidcStartWire",
    "BrowserOidcUnauthenticated", "BrowserOidcUnavailable", "FakeOidcAuthorizationCodeExchange",
    "HttpOidcAuthorizationCodeExchange", "OidcAuthorizationCodeExchangePort",
    "VerifiedBrowserOidcIdentity", "BROWSER_OIDC_TRANSACTION_TTL", "BROWSER_SESSION_TTL",
    "BrowserSessionApplication", "BrowserSessionCsrfForbidden", "BrowserSessionForbidden", "BrowserSessionProjection",
    "BrowserSessionUnauthenticated",
]
