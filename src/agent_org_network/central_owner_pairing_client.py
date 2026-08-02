"""Real HTTPS verifier for one-time Card Owner installation pairing."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from http.client import HTTPMessage
import ipaddress
import json
import math
import re
from typing import IO, Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import BaseModel, ConfigDict, SecretStr, field_validator

from agent_org_network.owner_credential_envelope import (
    OwnerCredentialEnvelope,
    X25519PublicJwk,
    device_key_thumbprint,
)


class CentralOwnerPairingUnavailable(Exception):
    pass


_OPAQUE = re.compile(r"[A-Za-z0-9_-]{32,256}")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
MAX_PAIRING_RESPONSE_BYTES = 64 * 1024


class OwnerPairingCode(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)
    intent_id: str
    value: SecretStr

    @field_validator("intent_id")
    @classmethod
    def _intent(cls, value: str) -> str:
        if _REF.fullmatch(value) is None:
            raise ValueError("bounded intent id required")
        return value

    @field_validator("value")
    @classmethod
    def _bounded(cls, value: SecretStr) -> SecretStr:
        if _OPAQUE.fullmatch(value.get_secret_value()) is None:
            raise ValueError("opaque pairing code required")
        return value


class RedeemedOwnerPairing(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)

    audience: Literal["owner-install"]
    org_id: str
    owner_id: str
    agent_id: str
    card_revision: int
    card_digest: str
    device_key_thumbprint: str
    identity_provider: str
    credential_id: str
    credential_generation: int
    expires_at: datetime
    envelope: OwnerCredentialEnvelope
    pairing_intent_digest: str
    issue_receipt_id: str
    issue_receipt_digest: str
    redeem_receipt_id: str
    redeem_receipt_digest: str

    @field_validator(
        "org_id", "owner_id", "agent_id", "identity_provider", "credential_id",
        "issue_receipt_id", "redeem_receipt_id",
    )
    @classmethod
    def _ref(cls, value: str) -> str:
        if _REF.fullmatch(value) is None:
            raise ValueError("bounded reference required")
        return value

    @field_validator(
        "card_digest", "pairing_intent_digest", "issue_receipt_digest",
        "redeem_receipt_digest",
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("sha256 required")
        return value

    @field_validator("card_revision", "credential_generation")
    @classmethod
    def _positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("positive revision/generation required")
        return value

    @field_validator("device_key_thumbprint")
    @classmethod
    def _thumbprint(cls, value: str) -> str:
        if not 40 <= len(value) <= 64 or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
            raise ValueError("canonical device thumbprint required")
        return value


class _Headers(Protocol):
    def get(self, name: str, default: str | None = None) -> str | None: ...


class _Response(Protocol):
    status: int

    @property
    def headers(self) -> _Headers: ...

    def read(self, amount: int = -1) -> bytes: ...
    def geturl(self) -> str: ...


class _Opener(Protocol):
    def __call__(self, request: Request, *, timeout: float) -> _Response: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> None:
        return None


_REAL_OPENER = build_opener(_NoRedirect())


def _open(request: Request, *, timeout: float) -> _Response:
    return _REAL_OPENER.open(request, timeout=timeout)


class ProductionCentralPairingVerifier:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 15.0,
        opener: _Opener = _open,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        parsed = urlsplit(base_url)
        hostname = parsed.hostname or ""
        try:
            loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            loopback = hostname.lower() == "localhost"
        if (
            parsed.scheme != "https"
            or not hostname
            or loopback
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 30
        ):
            raise CentralOwnerPairingUnavailable()
        self._base = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._open = opener
        self._clock = clock

    def redeem(
        self,
        code: OwnerPairingCode,
        *,
        device_public_key: X25519PublicJwk,
        idempotency_key: str,
    ) -> RedeemedOwnerPairing:
        if (
            type(code) is not OwnerPairingCode
            or type(device_public_key) is not X25519PublicJwk
            or _REF.fullmatch(idempotency_key) is None
        ):
            raise CentralOwnerPairingUnavailable()
        url = self._base + "/pairing/owner/redeem"
        body = json.dumps(
            {
                "intent_id": code.intent_id,
                "device_public_key": device_public_key.model_dump(mode="json"),
                "pairing_code": code.value.get_secret_value(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        request = Request(
            url,
            data=body,
            method="POST",
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "idempotency-key": idempotency_key,
            },
        )
        try:
            response = self._open(request, timeout=self._timeout)
            if (
                response.status != 200
                or response.geturl() != url
                or response.headers.get("content-type") != "application/json"
            ):
                raise CentralOwnerPairingUnavailable()
            payload = response.read(MAX_PAIRING_RESPONSE_BYTES + 1)
            if len(payload) > MAX_PAIRING_RESPONSE_BYTES:
                raise CentralOwnerPairingUnavailable()
            result = RedeemedOwnerPairing.model_validate_json(payload)
            if (
                result.expires_at.tzinfo is None
                or result.expires_at <= self._clock()
                or result.device_key_thumbprint
                != device_key_thumbprint(device_public_key)
                or result.envelope.aad.device_key_thumbprint
                != result.device_key_thumbprint
                or result.envelope.aad.credential_id != result.credential_id
                or result.envelope.aad.org_id != result.org_id
                or result.envelope.aad.owner_user_id != result.owner_id
                or result.envelope.aad.agent_card_id != result.agent_id
                or result.envelope.aad.credential_generation
                != result.credential_generation
                or result.envelope.aad.scope != ("author.read", "author.write")
                or datetime.fromisoformat(result.envelope.aad.expires_at)
                != result.expires_at
                or result.redeem_receipt_id != idempotency_key
            ):
                raise CentralOwnerPairingUnavailable()
            return result
        except CentralOwnerPairingUnavailable:
            raise
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
            raise CentralOwnerPairingUnavailable() from error
        except Exception as error:
            raise CentralOwnerPairingUnavailable() from error


__all__ = [
    "CentralOwnerPairingUnavailable",
    "MAX_PAIRING_RESPONSE_BYTES",
    "OwnerPairingCode",
    "ProductionCentralPairingVerifier",
    "RedeemedOwnerPairing",
]
