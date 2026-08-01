"""RFC 8628 device authorization adapter for one-time Central bootstrap.

This module deliberately owns transport and the local TTY prompt only.  It
does not persist a token, a device code, an email, or an OIDC claim.  The
bootstrap application owns attestation and Registry admission decisions.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
import math
import os
import time
from typing import Final, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from agent_org_network.central_bootstrap_admin import (
    BootstrapOidcDeviceDenied,
    BootstrapOidcDeviceUnavailable,
    VerifiedBootstrapIdentity,
)
from agent_org_network.oidc import OidcClaims, OidcProvider, OidcVerificationError


_DEVICE_GRANT: Final = "urn:ietf:params:oauth:grant-type:device_code"
_DEFAULT_TIMEOUT_SECONDS: Final = 10.0
_MAX_POLL_INTERVAL_SECONDS: Final = 60.0
_SLOW_DOWN_SECONDS: Final = 5.0
_MAX_DEVICE_LIFETIME_SECONDS: Final = 600
_MAX_POLL_ATTEMPTS: Final = 600


class BootstrapDeviceAuthorizationUnavailable(BootstrapOidcDeviceUnavailable):
    """A safe device-flow execution could not be completed."""


class BootstrapDeviceAuthorizationDenied(BootstrapOidcDeviceDenied):
    """The device authorization server or verified identity denied admission."""


@dataclass(frozen=True, slots=True)
class BootstrapHttpResponse:
    """A bounded response passed by an injectable HTTP requester seam."""

    status: int
    body: bytes


class BootstrapHttpRequester(Protocol):
    def __call__(
        self,
        url: str,
        *,
        method: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> BootstrapHttpResponse: ...


class BootstrapDevicePrompt(Protocol):
    """A prompt destination separate from normal command stdout/stderr."""

    def available(self) -> bool: ...

    def present(self, *, verification_uri: str, user_code: str) -> None: ...


class LocalTtyBootstrapDevicePrompt:
    """Print a one-time URI/code only to the invoking process's controlling TTY."""

    def available(self) -> bool:
        try:
            descriptor = os.open("/dev/tty", os.O_WRONLY)
        except OSError:
            return False
        else:
            os.close(descriptor)
            return True

    def present(self, *, verification_uri: str, user_code: str) -> None:
        if not _exact_https_url(verification_uri) or not _bounded_code(user_code):
            raise BootstrapDeviceAuthorizationUnavailable()
        try:
            descriptor = os.open("/dev/tty", os.O_WRONLY)
            with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as tty:
                tty.write("Complete Central bootstrap verification in your browser:\n")
                tty.write(f"{verification_uri}\n")
                tty.write(f"One-time code: {user_code}\n")
                tty.flush()
        except OSError as error:
            raise BootstrapDeviceAuthorizationUnavailable() from error


class HttpBootstrapOidcDeviceAuthorizer:
    """Strict RFC 8628 flow with OIDC discovery and post-token verification.

    `requester`, `clock`, `sleeper`, and `prompt` are explicit seams so all
    automated tests remain network- and wall-clock-free.  The default requester
    rejects HTTP redirects rather than following a server-selected endpoint.
    """

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        device_authorization_url: str,
        client_id: str,
        scope: str,
        oidc_provider: OidcProvider,
        requester: BootstrapHttpRequester | None = None,
        prompt: BootstrapDevicePrompt | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if (
            not _exact_https_url(issuer)
            or type(audience) is not str
            or not audience.strip()
            or not _exact_https_url(device_authorization_url)
            or not _bounded_reference(client_id)
            or not _valid_scope(scope)
            or not isinstance(timeout_seconds, float)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or not callable(getattr(oidc_provider, "verify", None))
            or requester is not None and not callable(requester)
            or prompt is not None
            and (
                not callable(getattr(prompt, "available", None))
                or not callable(getattr(prompt, "present", None))
            )
            or not callable(clock)
            or not callable(sleeper)
        ):
            raise BootstrapDeviceAuthorizationUnavailable()
        self._issuer = issuer.rstrip("/")
        self._audience = audience
        self._device_authorization_url = device_authorization_url
        self._client_id = client_id
        self._scope = scope
        self._oidc_provider = oidc_provider
        self._requester = requester or _stdlib_requester
        self._prompt = prompt or LocalTtyBootstrapDevicePrompt()
        self._clock = clock
        self._sleeper = sleeper
        self._timeout_seconds = timeout_seconds

    def authorize(
        self,
        *,
        provider_id: str,
        issuer: str,
        audience: str,
        device_authorization_ref: str,
    ) -> VerifiedBootstrapIdentity:
        """Acquire and verify an identity without allowing caller-selected endpoints."""
        if (
            not self._prompt.available()
            or not _bounded_reference(provider_id)
            or not _bounded_reference(device_authorization_ref)
            or issuer != self._issuer
            or audience != self._audience
        ):
            raise BootstrapDeviceAuthorizationUnavailable()
        token_endpoint = self._discover_token_endpoint()
        device = self._start_device_authorization()
        self._prompt.present(
            verification_uri=device.verification_uri,
            user_code=device.user_code,
        )
        claims = self._poll_for_claims(token_endpoint, device)
        if (
            claims.iss != self._issuer
            or claims.aud != self._audience
            or not claims.email_verified
            or not claims.sub
            or not claims.email
        ):
            raise BootstrapDeviceAuthorizationDenied()
        return VerifiedBootstrapIdentity(
            issuer=claims.iss,
            audience=claims.aud,
            subject=claims.sub,
            email=claims.email,
            email_verified=claims.email_verified,
        )

    def _discover_token_endpoint(self) -> str:
        discovery_url = f"{self._issuer}/.well-known/openid-configuration"
        response = self._request(
            discovery_url,
            method="GET",
            headers={"Accept": "application/json"},
            body=None,
        )
        if response.status != 200:
            raise BootstrapDeviceAuthorizationUnavailable()
        document = _json_object(response.body)
        issuer = document.get("issuer")
        advertised_device = document.get("device_authorization_endpoint")
        token_endpoint = document.get("token_endpoint")
        if (
            issuer != self._issuer
            or advertised_device != self._device_authorization_url
            or type(token_endpoint) is not str
            or not _exact_https_url(token_endpoint)
            or not _same_origin(token_endpoint, self._issuer)
        ):
            raise BootstrapDeviceAuthorizationUnavailable()
        return token_endpoint

    def _start_device_authorization(self) -> "_DeviceAuthorization":
        response = self._form_request(
            self._device_authorization_url,
            {"client_id": self._client_id, "scope": self._scope},
        )
        if response.status != 200:
            raise BootstrapDeviceAuthorizationUnavailable()
        document = _json_object(response.body)
        device_code = document.get("device_code")
        user_code = document.get("user_code")
        # `verification_uri_complete` normally embeds the one-time code in a
        # query string.  The code is displayed only via the TTY prompt below,
        # so use the base URI and never relax the endpoint URL policy for it.
        verification_uri = document.get("verification_uri")
        expires_in = document.get("expires_in")
        interval = document.get("interval", 5)
        if (
            type(device_code) is not str
            or not _bounded_code(device_code)
            or type(user_code) is not str
            or not _bounded_code(user_code)
            or type(verification_uri) is not str
            or not _exact_https_url(verification_uri)
            or type(expires_in) is not int
            or not 1 <= expires_in <= _MAX_DEVICE_LIFETIME_SECONDS
            or type(interval) is not int
            or not 1 <= interval <= _MAX_POLL_INTERVAL_SECONDS
        ):
            raise BootstrapDeviceAuthorizationUnavailable()
        return _DeviceAuthorization(
            device_code=device_code,
            user_code=user_code,
            verification_uri=verification_uri,
            expires_at=self._clock() + expires_in,
            interval=float(interval),
        )

    def _poll_for_claims(
        self, token_endpoint: str, device: "_DeviceAuthorization"
    ) -> OidcClaims:
        interval = device.interval
        polls = 0
        while polls < _MAX_POLL_ATTEMPTS:
            if self._clock() >= device.expires_at:
                raise BootstrapDeviceAuthorizationDenied()
            self._sleeper(interval)
            if self._clock() >= device.expires_at:
                raise BootstrapDeviceAuthorizationDenied()
            response = self._form_request(
                token_endpoint,
                {
                    "grant_type": _DEVICE_GRANT,
                    "device_code": device.device_code,
                    "client_id": self._client_id,
                },
            )
            polls += 1
            if response.status == 200:
                payload = _json_object(response.body)
                id_token = payload.get("id_token")
                if type(id_token) is not str or not id_token:
                    raise BootstrapDeviceAuthorizationUnavailable()
                try:
                    return self._oidc_provider.verify(id_token)
                except OidcVerificationError as error:
                    raise BootstrapDeviceAuthorizationDenied() from error
                except Exception as error:
                    raise BootstrapDeviceAuthorizationUnavailable() from error
            if response.status != 400:
                raise BootstrapDeviceAuthorizationUnavailable()
            error_code = _json_object(response.body).get("error")
            if error_code == "authorization_pending":
                continue
            if error_code == "slow_down":
                interval = min(interval + _SLOW_DOWN_SECONDS, _MAX_POLL_INTERVAL_SECONDS)
                continue
            if error_code in {"access_denied", "expired_token"}:
                raise BootstrapDeviceAuthorizationDenied()
            raise BootstrapDeviceAuthorizationUnavailable()
        raise BootstrapDeviceAuthorizationDenied()

    def _form_request(self, url: str, values: dict[str, str]) -> BootstrapHttpResponse:
        return self._request(
            url,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            body=urlencode(values).encode("ascii"),
        )

    def _request(
        self,
        url: str,
        *,
        method: str,
        headers: dict[str, str],
        body: bytes | None,
    ) -> BootstrapHttpResponse:
        if not _exact_https_url(url):
            raise BootstrapDeviceAuthorizationUnavailable()
        try:
            response = self._requester(
                url,
                method=method,
                headers=headers,
                body=body,
                timeout_seconds=self._timeout_seconds,
            )
        except BootstrapDeviceAuthorizationUnavailable:
            raise
        except Exception as error:
            raise BootstrapDeviceAuthorizationUnavailable() from error
        if (
            type(response) is not BootstrapHttpResponse
            or type(response.status) is not int
            or type(response.body) is not bytes
        ):
            raise BootstrapDeviceAuthorizationUnavailable()
        if len(response.body) > 64 * 1024:
            raise BootstrapDeviceAuthorizationUnavailable()
        return response


@dataclass(frozen=True, slots=True)
class _DeviceAuthorization:
    device_code: str
    user_code: str
    verification_uri: str
    expires_at: float
    interval: float


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> Request | None:
        _ = args, kwargs
        return None


def _stdlib_requester(
    url: str,
    *,
    method: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout_seconds: float,
) -> BootstrapHttpResponse:
    request = Request(url, data=body, headers=headers, method=method)
    opener = build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            return BootstrapHttpResponse(status=response.status, body=response.read(64 * 1024 + 1))
    except HTTPError as error:
        return BootstrapHttpResponse(status=error.code, body=error.read(64 * 1024 + 1))
    except (URLError, OSError, ValueError) as error:
        raise BootstrapDeviceAuthorizationUnavailable() from error


def _json_object(raw: bytes) -> dict[str, object]:
    try:
        value: object = json.loads(
            raw,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            object_pairs_hook=_no_duplicate_object,
        )
    except Exception as error:
        raise BootstrapDeviceAuthorizationUnavailable() from error
    if not isinstance(value, dict):
        raise BootstrapDeviceAuthorizationUnavailable()
    return dict(cast(dict[str, object], value))


def _no_duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate keys at every JSON object depth, not just top-level."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _exact_https_url(value: object) -> bool:
    if type(value) is not str or not value or any(ord(character) < 32 for character in value):
        return False
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and bool(parsed.netloc)
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )


def _same_origin(left: str, right: str) -> bool:
    a, b = urlsplit(left), urlsplit(right)
    return a.scheme == b.scheme and a.hostname == b.hostname and a.port == b.port


def _bounded_reference(value: object) -> bool:
    return type(value) is str and 1 <= len(value) <= 128 and all(
        character.isalnum() or character in "._:-" for character in value
    )


def _bounded_code(value: object) -> bool:
    return type(value) is str and 1 <= len(value) <= 4096 and all(
        32 <= ord(character) < 127 for character in value
    )


def _valid_scope(value: object) -> bool:
    return type(value) is str and bool(value) and all(
        bool(part) and all(character.isalnum() or character in "._:-/" for character in part)
        for part in value.split(" ")
    )


__all__ = [
    "BootstrapDeviceAuthorizationDenied",
    "BootstrapDeviceAuthorizationUnavailable",
    "BootstrapDevicePrompt",
    "BootstrapHttpRequester",
    "BootstrapHttpResponse",
    "HttpBootstrapOidcDeviceAuthorizer",
    "LocalTtyBootstrapDevicePrompt",
    "NoRedirect",
]
