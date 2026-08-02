from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from typing import cast
from urllib.error import HTTPError
from urllib.request import Request

import pytest
from pydantic import SecretStr, ValidationError

from agent_org_network.central_owner_pairing_client import (
    CentralOwnerPairingUnavailable,
    OwnerPairingCode,
    ProductionCentralPairingVerifier,
    RedeemedOwnerPairing,
)
from agent_org_network.owner_credential_envelope import (
    CredentialEnvelopeAad,
    X25519PublicJwk,
    device_key_thumbprint,
    encrypt_owner_credential_with_verifier,
    generate_device_keypair,
)
from agent_org_network.owner_pairing_digest import (
    owner_pairing_redeem_request_digest,
)

_PRIVATE, PUBLIC = generate_device_keypair()
_OTHER_PRIVATE, OTHER_PUBLIC = generate_device_keypair()


class _Headers:
    def __init__(self, content_type: str = "application/json") -> None:
        self._content_type = content_type

    def get(self, name: str, default: str | None = None) -> str | None:
        return self._content_type if name == "content-type" else default


class _Response:
    def __init__(
        self,
        payload: object,
        *,
        url: str = "https://central.example/pairing/owner/redeem",
        status: int = 200,
        content_type: str = "application/json",
    ) -> None:
        self.status = status
        self._headers = _Headers(content_type)
        self._payload = json.dumps(payload).encode()
        self._url = url

    def read(self, amount: int = -1) -> bytes:
        return self._payload[:amount]

    @property
    def headers(self) -> _Headers:
        return self._headers

    def geturl(self) -> str:
        return self._url


def _code(value: str = "p" * 32) -> OwnerPairingCode:
    return OwnerPairingCode(intent_id="intent-1", value=SecretStr(value))


def _payload(*, device_public_key: X25519PublicJwk = PUBLIC) -> dict[str, object]:
    issued = datetime.now(UTC).replace(microsecond=0)
    aad = CredentialEnvelopeAad(
        agent_card_id="support",
        credential_generation=1,
        credential_id="credential-1",
        device_key_thumbprint=device_key_thumbprint(device_public_key),
        expires_at=(issued + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        issued_at=issued.strftime("%Y-%m-%dT%H:%M:%SZ"),
        org_id="acme",
        owner_user_id="owner",
        scope=("author.read", "author.write"),
    )
    envelope = encrypt_owner_credential_with_verifier(
        aad,
        device_public_key,
        verifier=lambda _secret: ("key-1", "a" * 64),
    ).envelope
    return {
        "audience": "owner-install",
        "credential_id": "credential-1",
        "org_id": "acme",
        "owner_id": "owner",
        "agent_id": "support",
        "card_revision": 2,
        "card_digest": "a" * 64,
        "device_key_thumbprint": device_key_thumbprint(device_public_key),
        "identity_provider": "corp",
        "credential_generation": 1,
        "expires_at": envelope.aad.expires_at,
        "redeem_request_digest": owner_pairing_redeem_request_digest(
            intent_id="intent-1",
            idempotency_key="pair-1",
            device_key_thumbprint=device_key_thumbprint(device_public_key),
        ),
        "pairing_intent_digest": "b" * 64,
        "issue_receipt_id": "issue-1",
        "issue_receipt_digest": "c" * 64,
        "redeem_receipt_id": "pair-1",
        "redeem_receipt_digest": "d" * 64,
        "envelope": envelope.model_dump(mode="json"),
    }


def test_real_verifier는_exact_TLS_redeem_wire와_secret_repr0이다() -> None:
    seen: list[Request] = []

    def open_request(request: Request, *, timeout: float) -> _Response:
        seen.append(request)
        assert timeout == 3
        return _Response(_payload())

    verifier = ProductionCentralPairingVerifier(
        "https://central.example", timeout_seconds=3, opener=open_request
    )
    result = verifier.redeem(
        _code(), device_public_key=PUBLIC, idempotency_key="pair-1"
    )
    assert result.org_id == "acme"
    assert result.agent_id == "support"
    assert result.device_key_thumbprint == device_key_thumbprint(PUBLIC)
    assert "p" * 32 not in repr(_code())
    request = seen[0]
    assert request.full_url == "https://central.example/pairing/owner/redeem"
    assert request.get_header("Idempotency-key") == "pair-1"
    wire = json.loads(cast(bytes, request.data))
    assert wire == {
        "intent_id": "intent-1",
        "device_public_key": PUBLIC.model_dump(mode="json"),
        "pairing_code": "p" * 32,
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://central.example",
        "https://localhost",
        "https://127.0.0.1",
        "https://user@central.example",
        "https://central.example/path",
        "https://central.example?x=1",
    ],
)
def test_nonproduction_url은failclosed(url: str) -> None:
    with pytest.raises(CentralOwnerPairingUnavailable):
        ProductionCentralPairingVerifier(url)


@pytest.mark.parametrize("value", ["short", "p" * 31, "p" * 257, "p" * 31 + ";"])
def test_pairing_code는SecretStr_opaque_exact다(value: str) -> None:
    with pytest.raises(ValidationError):
        _code(value)


@pytest.mark.parametrize(
    ("status", "content_type", "url"),
    [
        (302, "application/json", "https://central.example/pairing/owner/redeem"),
        (200, "text/plain", "https://central.example/pairing/owner/redeem"),
        (200, "application/json", "https://evil.example/steal"),
    ],
)
def test_status_content_type_final_url_mismatch는failclosed(
    status: int, content_type: str, url: str
) -> None:
    def open_mismatch(request: Request, *, timeout: float) -> _Response:
        return _Response(
            _payload(), status=status, content_type=content_type, url=url
        )

    verifier = ProductionCentralPairingVerifier(
        "https://central.example",
        opener=open_mismatch,
    )
    with pytest.raises(CentralOwnerPairingUnavailable):
        verifier.redeem(
            _code(), device_public_key=PUBLIC, idempotency_key="pair-1"
        )


@pytest.mark.parametrize("shape", ["extra", "missing", "expired"])
def test_response_shape와_expiry는exact_failclosed(shape: str) -> None:
    payload = _payload()
    if shape == "extra":
        payload["unexpected"] = "x"
    elif shape == "missing":
        payload.pop("card_digest")
    else:
        payload["expires_at"] = (
            datetime.now(UTC) - timedelta(seconds=1)
        ).isoformat()

    def open_shape(request: Request, *, timeout: float) -> _Response:
        return _Response(payload)

    verifier = ProductionCentralPairingVerifier(
        "https://central.example", opener=open_shape
    )
    with pytest.raises(CentralOwnerPairingUnavailable):
        verifier.redeem(
            _code(), device_public_key=PUBLIC, idempotency_key="pair-1"
        )


def test_strict_response가_다른_device에_binding되면_failclosed한다() -> None:
    payload = _payload(device_public_key=OTHER_PUBLIC)

    def open_wrong_device(request: Request, *, timeout: float) -> _Response:
        return _Response(payload)

    verifier = ProductionCentralPairingVerifier(
        "https://central.example", opener=open_wrong_device
    )
    with pytest.raises(CentralOwnerPairingUnavailable):
        verifier.redeem(
            _code(), device_public_key=PUBLIC, idempotency_key="pair-1"
        )


def test_sorted_valid_but_broader_scope도failclosed다() -> None:
    payload = _payload()
    envelope = cast(dict[str, object], payload["envelope"])
    aad = cast(dict[str, object], envelope["aad"])
    aad["scope"] = ["author.publish", "author.read", "author.write"]
    assert (
        RedeemedOwnerPairing.model_validate_json(
            json.dumps(payload, separators=(",", ":"))
        ).envelope.aad.scope
        == ("author.publish", "author.read", "author.write")
    )

    def open_scope(request: Request, *, timeout: float) -> _Response:
        return _Response(payload)

    verifier = ProductionCentralPairingVerifier(
        "https://central.example", opener=open_scope
    )
    with pytest.raises(CentralOwnerPairingUnavailable):
        verifier.redeem(
            _code(), device_public_key=PUBLIC, idempotency_key="pair-1"
        )


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("nan"), float("inf"), 30.1])
def test_timeout은_finite한_0초초과_30초이하다(timeout: float) -> None:
    with pytest.raises(CentralOwnerPairingUnavailable):
        ProductionCentralPairingVerifier(
            "https://central.example", timeout_seconds=timeout
        )


def test_actual_redirect_handler는_second_request와credential_leak0이다() -> None:
    source_count = 0
    target_count = 0
    target_bodies: list[bytes] = []

    class Target(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            nonlocal target_count
            target_count += 1
            target_bodies.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return None

    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    target_thread = Thread(target=target.serve_forever, daemon=True)
    target_thread.start()

    class Source(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            nonlocal source_count
            source_count += 1
            self.send_response(302)
            self.send_header(
                "Location", f"http://127.0.0.1:{target.server_address[1]}/steal"
            )
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return None

    source = ThreadingHTTPServer(("127.0.0.1", 0), Source)
    source_thread = Thread(target=source.serve_forever, daemon=True)
    source_thread.start()
    try:
        import agent_org_network.central_owner_pairing_client as module

        request = Request(
            f"http://127.0.0.1:{source.server_address[1]}/pair",
            data=b'{"pairing_code":"SECRET"}',
            method="POST",
            headers={"Idempotency-Key": "pair-1"},
        )
        with pytest.raises(HTTPError) as raised:
            module._REAL_OPENER.open(request, timeout=2)  # pyright: ignore[reportPrivateUsage]
        assert raised.value.code == 302
        assert source_count == 1
        assert target_count == 0
        assert target_bodies == []
    finally:
        source.shutdown()
        target.shutdown()
        source.server_close()
        target.server_close()
        source_thread.join(timeout=2)
        target_thread.join(timeout=2)
