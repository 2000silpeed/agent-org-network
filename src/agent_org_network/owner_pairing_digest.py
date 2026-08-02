"""Secret-free canonical digest shared by Central and Owner pairing clients."""

from __future__ import annotations

from hashlib import sha256
import json
import re


_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_THUMBPRINT = re.compile(r"[A-Za-z0-9_-]{43}")


class OwnerPairingDigestUnavailable(ValueError):
    """The public pairing digest projection is malformed."""


def owner_pairing_redeem_request_digest(
    *, intent_id: str, idempotency_key: str, device_key_thumbprint: str
) -> str:
    """Digest only the public redeem command projection.

    The pairing code remains an independent Central authentication input and is
    intentionally absent from this projection, so Owner can persist the CAS
    fence before making the network call.
    """
    if (
        type(intent_id) is not str
        or type(idempotency_key) is not str
        or type(device_key_thumbprint) is not str
        or _REF.fullmatch(intent_id) is None
        or _REF.fullmatch(idempotency_key) is None
        or _THUMBPRINT.fullmatch(device_key_thumbprint) is None
    ):
        raise OwnerPairingDigestUnavailable()
    projection = {
        "device_key_thumbprint": device_key_thumbprint,
        "idempotency_key": idempotency_key,
        "intent_id": intent_id,
        "kind": "owner-pairing.redeem.v1",
    }
    encoded = json.dumps(
        projection, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


__all__ = [
    "OwnerPairingDigestUnavailable",
    "owner_pairing_redeem_request_digest",
]
