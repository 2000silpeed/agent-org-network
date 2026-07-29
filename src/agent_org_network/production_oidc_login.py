"""Production OIDC authorization-code + PKCE login boundary.

Authorization codes and tokens are request-local only.  The transaction store
retains only random correlation material, nonce and PKCE verifier for a short
period; consumed and expired transactions cannot be replayed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import base64
import hashlib
import json
import secrets
import threading
from typing import Any, Protocol, cast
import urllib.parse
import urllib.request

from agent_org_network.oidc import OidcProvider, OidcVerificationError
from agent_org_network.production_identity_sessions import VerifiedEmailIdentityProof


class ProductionOidcLoginUnavailable(Exception):
    """The login proof could not be safely completed."""


class AuthorizationCodeOidcProvider(Protocol):
    provider_id: str
    issuer: str

    def exchange(
        self, *, code: str, redirect_uri: str, code_verifier: str, expected_nonce: str
    ) -> VerifiedEmailIdentityProof: ...


@dataclass(frozen=True)
class _Transaction:
    state: str
    browser_binding: str = field(repr=False)
    nonce: str = field(repr=False)
    code_verifier: str = field(repr=False)
    expires_at: datetime


class OidcLoginTransactions:
    """Process-local, short-lived and single-use OIDC transaction repository."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        ttl: timedelta = timedelta(minutes=5),
        random_token: Callable[[], str] = lambda: secrets.token_urlsafe(32),
    ) -> None:
        if ttl <= timedelta(0) or ttl > timedelta(minutes=10):
            raise ProductionOidcLoginUnavailable()
        self._clock = clock
        self._ttl = ttl
        self._random = random_token
        self._items: dict[str, _Transaction] = {}
        self._lock = threading.Lock()

    def begin(self) -> _Transaction:
        values = tuple(self._random() for _ in range(4))
        if any(len(value) < 32 for value in values) or len(set(values)) != 4:
            raise ProductionOidcLoginUnavailable()
        transaction = _Transaction(
            state=values[0],
            browser_binding=values[1],
            nonce=values[2],
            code_verifier=values[3],
            expires_at=self._clock() + self._ttl,
        )
        with self._lock:
            self._items[transaction.state] = transaction
        return transaction

    def consume(self, *, state: str, browser_binding: str) -> _Transaction:
        # Pop first: every callback attempt consumes the state, including a bad
        # browser binding, so guessing cannot leave a transaction replayable.
        with self._lock:
            transaction = self._items.pop(state, None)
        if (
            transaction is None
            or not secrets.compare_digest(transaction.browser_binding, browser_binding)
            or transaction.expires_at <= self._clock()
        ):
            raise ProductionOidcLoginUnavailable()
        return transaction


class HttpAuthorizationCodeOidcProvider:
    """Provider-neutral token endpoint adapter with verified ID-token projection."""

    def __init__(
        self,
        *,
        provider_id: str,
        issuer: str,
        client_id: str,
        client_secret: str | None,
        token_url: str,
        id_token_verifier: OidcProvider,
        post_form: Callable[[str, bytes], dict[str, Any]] | None = None,
    ) -> None:
        if (
            not provider_id
            or not client_id
            or not _is_secure_url(token_url)
            or not _is_secure_url(issuer)
        ):
            raise ProductionOidcLoginUnavailable()
        self.provider_id = provider_id
        self.issuer = issuer
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_url = token_url
        self._verifier = id_token_verifier
        self._post_form = post_form or _post_form

    def exchange(
        self, *, code: str, redirect_uri: str, code_verifier: str, expected_nonce: str
    ) -> VerifiedEmailIdentityProof:
        if not _is_secure_url(redirect_uri):
            raise ProductionOidcLoginUnavailable()
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self._client_id,
            "code_verifier": code_verifier,
        }
        if self._client_secret is not None:
            form["client_secret"] = self._client_secret
        try:
            response = self._post_form(
                self._token_url, urllib.parse.urlencode(form).encode("ascii")
            )
            raw_id_token = response.get("id_token")
            if not isinstance(raw_id_token, str):
                raise ProductionOidcLoginUnavailable()
            claims = self._verifier.verify(raw_id_token)
            payload = _verified_payload(raw_id_token)
            nonce = payload.get("nonce")
            if (
                not isinstance(nonce, str)
                or not secrets.compare_digest(nonce, expected_nonce)
                or claims.iss != self.issuer
                or claims.aud != self._client_id
                or not claims.email_verified
            ):
                raise ProductionOidcLoginUnavailable()
            return VerifiedEmailIdentityProof(
                provider_id=self.provider_id,
                issuer=claims.iss,
                email=claims.email,
                email_verified=True,
            )
        except (OidcVerificationError, ValueError, KeyError, TypeError):
            raise ProductionOidcLoginUnavailable() from None


def authorization_url(
    *,
    endpoint: str,
    client_id: str,
    redirect_uri: str,
    transaction: _Transaction,
) -> str:
    if (
        not client_id
        or not _is_secure_url(endpoint, query_allowed=False)
        or not _is_secure_url(redirect_uri)
    ):
        raise ProductionOidcLoginUnavailable()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(transaction.code_verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": "openid email",
            "state": transaction.state,
            "nonce": transaction.nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{endpoint}?{query}"


def _is_secure_url(value: str, *, query_allowed: bool = True) -> bool:
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
        and (query_allowed or not parsed.query)
    )


def _post_form(url: str, body: bytes) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            parsed: object = json.loads(response.read())
    except Exception:
        raise ProductionOidcLoginUnavailable() from None
    if not isinstance(parsed, dict):
        raise ProductionOidcLoginUnavailable()
    return cast("dict[str, Any]", parsed)


def _verified_payload(id_token: str) -> dict[str, Any]:
    # Only used after the configured verifier has authenticated this exact token.
    parts = id_token.split(".")
    if len(parts) != 3:
        raise ProductionOidcLoginUnavailable()
    padding = "=" * (-len(parts[1]) % 4)
    parsed: object = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
    if not isinstance(parsed, dict):
        raise ProductionOidcLoginUnavailable()
    return cast("dict[str, Any]", parsed)


__all__ = [
    "AuthorizationCodeOidcProvider",
    "HttpAuthorizationCodeOidcProvider",
    "OidcLoginTransactions",
    "ProductionOidcLoginUnavailable",
    "authorization_url",
]
