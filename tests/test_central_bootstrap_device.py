"""Deterministic RFC 8628 adapter contract; no real IdP is contacted."""

from __future__ import annotations

import json

import pytest

from agent_org_network.central_bootstrap_device import (
    BootstrapDeviceAuthorizationDenied,
    BootstrapDeviceAuthorizationUnavailable,
    BootstrapHttpResponse,
    HttpBootstrapOidcDeviceAuthorizer,
    NoRedirect,
)
from agent_org_network.oidc import FakeOidcProvider, OidcClaims


ISSUER = "https://idp.example.test"
DEVICE = f"{ISSUER}/device_authorization"
TOKEN = f"{ISSUER}/token"


class _Prompt:
    def __init__(self, available: bool = True) -> None:
        self._available = available
        self.presented: list[tuple[str, str]] = []

    def available(self) -> bool:
        return self._available

    def present(self, *, verification_uri: str, user_code: str) -> None:
        self.presented.append((verification_uri, user_code))


def _json(status: int, payload: dict[str, object]) -> BootstrapHttpResponse:
    return BootstrapHttpResponse(status=status, body=json.dumps(payload).encode())


def _authorizer(
    responses: list[BootstrapHttpResponse],
    *,
    prompt: _Prompt,
    times: list[float],
    sleeps: list[float] | None = None,
) -> tuple[HttpBootstrapOidcDeviceAuthorizer, list[tuple[str, str]]]:
    calls: list[tuple[str, str]] = []

    def requester(
        url: str, *, method: str, headers: dict[str, str], body: bytes | None, timeout_seconds: float
    ) -> BootstrapHttpResponse:
        _ = headers, body, timeout_seconds
        calls.append((method, url))
        return responses.pop(0)

    def clock() -> float:
        return times[0]

    def sleeper(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)
        times[0] += seconds

    oidc = FakeOidcProvider(
        {
            "verified-token": OidcClaims(
                sub="subject-1", email="root@example.test", email_verified=True,
                iss=ISSUER, aud="aon-central",
            )
        }
    )
    return (
        HttpBootstrapOidcDeviceAuthorizer(
            issuer=ISSUER, audience="aon-central", device_authorization_url=DEVICE,
            client_id="aon-bootstrap", scope="openid email", oidc_provider=oidc,
            requester=requester, prompt=prompt, clock=clock, sleeper=sleeper,
        ),
        calls,
    )


def _discovery() -> BootstrapHttpResponse:
    return _json(200, {"issuer": ISSUER, "device_authorization_endpoint": DEVICE, "token_endpoint": TOKEN})


def _device(*, expires_in: int = 120, interval: int = 1) -> BootstrapHttpResponse:
    return _json(200, {
        "device_code": "opaque-device-code", "user_code": "ABCD-EFGH",
        "verification_uri": "https://verify.example.test/activate",
        "verification_uri_complete": "https://verify.example.test/activate?user_code=ABCD-EFGH",
        "expires_in": expires_in, "interval": interval,
    })


def test_valid_rfc8628_flow_uses_base_verification_uri_and_reverifies_id_token() -> None:
    prompt = _Prompt()
    authorizer, calls = _authorizer(
        [_discovery(), _device(), _json(400, {"error": "authorization_pending"}), _json(200, {"id_token": "verified-token"})],
        prompt=prompt,
        times=[0.0],
    )

    identity = authorizer.authorize(
        provider_id="company-oidc", issuer=ISSUER, audience="aon-central",
        device_authorization_ref="opaque-device-reference",
    )

    assert identity.subject == "subject-1"
    assert prompt.presented == [("https://verify.example.test/activate", "ABCD-EFGH")]
    assert calls == [("GET", f"{ISSUER}/.well-known/openid-configuration"), ("POST", DEVICE), ("POST", TOKEN), ("POST", TOKEN)]


def test_non_tty_stops_before_network_and_device_denial_is_distinct() -> None:
    no_tty = _Prompt(available=False)
    authorizer, calls = _authorizer([_discovery()], prompt=no_tty, times=[0.0])
    with pytest.raises(BootstrapDeviceAuthorizationUnavailable):
        authorizer.authorize(provider_id="company-oidc", issuer=ISSUER, audience="aon-central", device_authorization_ref="ref")
    assert calls == []

    prompt = _Prompt()
    denied, _ = _authorizer([_discovery(), _device(), _json(400, {"error": "access_denied"})], prompt=prompt, times=[0.0])
    with pytest.raises(BootstrapDeviceAuthorizationDenied):
        denied.authorize(provider_id="company-oidc", issuer=ISSUER, audience="aon-central", device_authorization_ref="ref")


def test_discovery_endpoint_drift_fails_closed_before_prompt() -> None:
    prompt = _Prompt()
    authorizer, _ = _authorizer(
        [_json(200, {"issuer": ISSUER, "device_authorization_endpoint": "https://evil.example/device", "token_endpoint": TOKEN})],
        prompt=prompt,
        times=[0.0],
    )
    with pytest.raises(BootstrapDeviceAuthorizationUnavailable):
        authorizer.authorize(provider_id="company-oidc", issuer=ISSUER, audience="aon-central", device_authorization_ref="ref")
    assert prompt.presented == []


@pytest.mark.parametrize(
    "raw",
    (
        b'{"issuer":"https://idp.example.test","issuer":"https://idp.example.test"}',
        b'{"issuer":NaN}',
        b"{",
    ),
)
def test_discovery_malformed_or_duplicate_json_fails_closed(raw: bytes) -> None:
    prompt = _Prompt()
    authorizer, _ = _authorizer([BootstrapHttpResponse(status=200, body=raw)], prompt=prompt, times=[0.0])
    with pytest.raises(BootstrapDeviceAuthorizationUnavailable):
        authorizer.authorize(provider_id="company-oidc", issuer=ISSUER, audience="aon-central", device_authorization_ref="ref")
    assert prompt.presented == []


@pytest.mark.parametrize(
    "response",
    (
        BootstrapHttpResponse(status=500, body=b"{}"),
        BootstrapHttpResponse(status=200, body=b"x" * (64 * 1024 + 1)),
    ),
)
def test_discovery_non_success_or_oversize_response_fails_closed(response: BootstrapHttpResponse) -> None:
    authorizer, _ = _authorizer([response], prompt=_Prompt(), times=[0.0])
    with pytest.raises(BootstrapDeviceAuthorizationUnavailable):
        authorizer.authorize(provider_id="company-oidc", issuer=ISSUER, audience="aon-central", device_authorization_ref="ref")


def test_device_and_token_duplicate_json_non_400_and_expiry_fail_closed() -> None:
    duplicate_device = BootstrapHttpResponse(
        status=200,
        body=(b'{"device_code":"one","device_code":"two","user_code":"ABCD",'
              b'"verification_uri":"https://verify.example.test/activate","expires_in":120}'),
    )
    authorizer, _ = _authorizer([_discovery(), duplicate_device], prompt=_Prompt(), times=[0.0])
    with pytest.raises(BootstrapDeviceAuthorizationUnavailable):
        authorizer.authorize(provider_id="company-oidc", issuer=ISSUER, audience="aon-central", device_authorization_ref="ref")

    bad_token, _ = _authorizer([_discovery(), _device(), _json(500, {})], prompt=_Prompt(), times=[0.0])
    with pytest.raises(BootstrapDeviceAuthorizationUnavailable):
        bad_token.authorize(provider_id="company-oidc", issuer=ISSUER, audience="aon-central", device_authorization_ref="ref")

    duplicate_token, _ = _authorizer(
        [_discovery(), _device(), BootstrapHttpResponse(status=400, body=b'{"error":"authorization_pending","error":"slow_down"}')],
        prompt=_Prompt(), times=[0.0],
    )
    with pytest.raises(BootstrapDeviceAuthorizationUnavailable):
        duplicate_token.authorize(provider_id="company-oidc", issuer=ISSUER, audience="aon-central", device_authorization_ref="ref")

    too_long, _ = _authorizer([_discovery(), _device(expires_in=601)], prompt=_Prompt(), times=[0.0])
    with pytest.raises(BootstrapDeviceAuthorizationUnavailable):
        too_long.authorize(provider_id="company-oidc", issuer=ISSUER, audience="aon-central", device_authorization_ref="ref")

    expired, calls = _authorizer([_discovery(), _device(expires_in=1, interval=1)], prompt=_Prompt(), times=[0.0])
    with pytest.raises(BootstrapDeviceAuthorizationDenied):
        expired.authorize(provider_id="company-oidc", issuer=ISSUER, audience="aon-central", device_authorization_ref="ref")
    assert calls == [("GET", f"{ISSUER}/.well-known/openid-configuration"), ("POST", DEVICE)]


def test_slow_down_clamps_and_explicit_expired_token_are_denied() -> None:
    sleeps: list[float] = []
    slow, _ = _authorizer(
        [_discovery(), _device(expires_in=180, interval=60), _json(400, {"error": "slow_down"}), _json(200, {"id_token": "verified-token"})],
        prompt=_Prompt(), times=[0.0], sleeps=sleeps,
    )
    assert slow.authorize(provider_id="company-oidc", issuer=ISSUER, audience="aon-central", device_authorization_ref="ref").email_verified
    assert sleeps == [60.0, 60.0]

    increased_sleeps: list[float] = []
    increased, _ = _authorizer(
        [_discovery(), _device(), _json(400, {"error": "slow_down"}), _json(200, {"id_token": "verified-token"})],
        prompt=_Prompt(), times=[0.0], sleeps=increased_sleeps,
    )
    assert increased.authorize(provider_id="company-oidc", issuer=ISSUER, audience="aon-central", device_authorization_ref="ref").email_verified
    assert increased_sleeps == [1.0, 6.0]

    expired, _ = _authorizer([_discovery(), _device(), _json(400, {"error": "expired_token"})], prompt=_Prompt(), times=[0.0])
    with pytest.raises(BootstrapDeviceAuthorizationDenied):
        expired.authorize(provider_id="company-oidc", issuer=ISSUER, audience="aon-central", device_authorization_ref="ref")


def test_constructor_and_redirect_seams_fail_closed() -> None:
    oidc = FakeOidcProvider()
    for timeout in (float("nan"), float("inf"), float("-inf"), 0.0):
        with pytest.raises(BootstrapDeviceAuthorizationUnavailable):
            HttpBootstrapOidcDeviceAuthorizer(
                issuer=ISSUER, audience="aon-central", device_authorization_url=DEVICE,
                client_id="aon-bootstrap", scope="openid", oidc_provider=oidc,
                timeout_seconds=timeout,
            )
    with pytest.raises(BootstrapDeviceAuthorizationUnavailable):
        HttpBootstrapOidcDeviceAuthorizer(
            issuer=ISSUER, audience="", device_authorization_url=DEVICE,
            client_id="aon-bootstrap", scope="openid", oidc_provider=oidc,
        )
    with pytest.raises(BootstrapDeviceAuthorizationUnavailable):
        HttpBootstrapOidcDeviceAuthorizer(
            issuer=ISSUER, audience="aon-central", device_authorization_url=DEVICE,
            client_id="aon-bootstrap", scope="openid", oidc_provider=object(),  # type: ignore[arg-type]
        )

    def timeout_requester(
        url: str,
        *,
        method: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> BootstrapHttpResponse:
        _ = url, method, headers, body, timeout_seconds
        raise TimeoutError

    timed_out = HttpBootstrapOidcDeviceAuthorizer(
        issuer=ISSUER, audience="aon-central", device_authorization_url=DEVICE,
        client_id="aon-bootstrap", scope="openid", oidc_provider=oidc,
        requester=timeout_requester, prompt=_Prompt(),
    )
    with pytest.raises(BootstrapDeviceAuthorizationUnavailable):
        timed_out.authorize(provider_id="company-oidc", issuer=ISSUER, audience="aon-central", device_authorization_ref="ref")
    assert NoRedirect().redirect_request() is None
