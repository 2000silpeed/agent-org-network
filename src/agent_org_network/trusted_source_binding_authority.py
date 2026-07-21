"""HMAC-backed ADR 0059 Authority test/support implementation."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, final

from agent_org_network.reciprocal_review import SourceBindingAuthorizationEnvelopeV7
from agent_org_network.source_binding_authorization import (
    SourceBindingAuthorizationDenied,
    SourceBindingAuthorizationUnavailable,
)

_MINT = secrets.token_urlsafe(32)


@dataclass(frozen=True, init=False)
class _VerifiedSourceBindingAuthorization:
    receipt_id: str
    payload_digest: str
    valid_until: datetime

    def __init__(self, token: str, receipt_id: str, payload_digest: str, valid_until: datetime) -> None:
        if not hmac.compare_digest(token, _MINT):
            raise TypeError("Authority-only capability")
        object.__setattr__(self, "receipt_id", receipt_id)
        object.__setattr__(self, "payload_digest", payload_digest)
        object.__setattr__(self, "valid_until", valid_until)


class TrustedSourceBindingAuthority:
    def __init__(self, keys: Mapping[str, bytes], current: Callable[[SourceBindingAuthorizationEnvelopeV7, str, datetime], bool]) -> None:
        self._keys = dict(keys)
        self._current = current

    def verify_current(self, *, receipt: SourceBindingAuthorizationEnvelopeV7, purpose: str, readback: object | None = None, db_now: datetime) -> _VerifiedSourceBindingAuthorization | SourceBindingAuthorizationDenied | SourceBindingAuthorizationUnavailable:
        key = self._keys.get(receipt.key_id)
        if key is None or db_now >= receipt.expires_at or db_now >= receipt.drift_expires_at:
            return SourceBindingAuthorizationDenied("not_found_or_denied")
        payload = receipt.model_dump(mode="json", exclude={"signature"})
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        if not hmac.compare_digest(receipt.signature, hmac.new(key, raw, hashlib.sha256).hexdigest()):
            return SourceBindingAuthorizationDenied("source_binding_receipt_invalid")
        try:
            if not self._current(receipt, purpose, db_now):
                return SourceBindingAuthorizationDenied("not_found_or_denied")
        except Exception:
            return SourceBindingAuthorizationUnavailable("source_binding_authority_unavailable")
        return _VerifiedSourceBindingAuthorization(_MINT, receipt.receipt_id, receipt.payload_digest, receipt.expires_at)


_WIRING_MINT = secrets.token_urlsafe(32)


@final
class SourceBindingAuthorityCapability:
    def __init__(self, token: str, wiring: SourceBindingAuthorityWiring, handle: object, authority: object) -> None:
        if not hmac.compare_digest(token, _WIRING_MINT):
            raise TypeError("ProductionAuthorityComposition-only capability")
        self._wiring = wiring
        self._handle = handle
        self._authority = authority

    def verify_current(
        self, *, receipt: SourceBindingAuthorizationEnvelopeV7, purpose: str, readback: object | None = None, db_now: datetime
    ) -> _VerifiedSourceBindingAuthorization | SourceBindingAuthorizationDenied | SourceBindingAuthorizationUnavailable:
        if not self.is_live():
            return SourceBindingAuthorizationDenied("production_bootstrap_closed_or_mismatched")
        return self._wiring.verify_from_capability(receipt=receipt, purpose=purpose, readback=readback, db_now=db_now)

    def is_live(self) -> bool:
        handle = self._handle
        return bool(
            getattr(handle, "authority_capability", None) is self._authority
            and not getattr(handle, "_source_binding_closed", True)
            and getattr(self._authority, "_state", None) == "claimed"
            and getattr(handle, "source_binding_wiring", self._wiring) is self._wiring
            and self._wiring.has_live_wiring()
        )

    def matches_sqlite_target(self, target: str | Path) -> bool:
        return self.is_live() and self._wiring.matches_sqlite_target(target)

    def matches_source_ref(self, source_ref: str) -> bool:
        return self.is_live() and self._wiring.matches_source_ref(source_ref)


@final
class SourceBindingAuthorityWiring:
    """Bootstrap-only central registry/wiring; constructor is deliberately sealed."""
    def __init__(self, token: str, *, issuer_registry: Mapping[str, bytes], resolver: Callable[[SourceBindingAuthorizationEnvelopeV7, str, datetime], bool], database_wiring: object, source_wiring: object) -> None:
        if not hmac.compare_digest(token, _WIRING_MINT):
            raise TypeError("production bootstrap-only source binding wiring")
        self._registry = dict(issuer_registry)
        self._resolver = resolver
        self._database_wiring = database_wiring
        self._source_wiring = source_wiring

    def open_for_bootstrap(self, handle: object, authority: object) -> SourceBindingAuthorityCapability | None:
        if not self.has_live_wiring() or getattr(handle, "authority_capability", None) is not authority or getattr(authority, "_state", None) != "claimed" or getattr(handle, "_source_binding_closed", False):
            return None
        return SourceBindingAuthorityCapability(_WIRING_MINT, self, handle, authority)

    def has_live_wiring(self) -> bool:
        return self._canonical_target(self._database_wiring) is not None and type(self._source_wiring) is str and bool(self._source_wiring)

    @staticmethod
    def _canonical_target(value: object) -> Path | None:
        if not isinstance(value, (str, Path)):
            return None
        raw = str(value)
        if not raw or raw == ":memory:" or raw.startswith("file:") or "mode=memory" in raw:
            return None
        path = Path(raw)
        if not path.is_absolute():
            return None
        try:
            return path.resolve(strict=False)
        except (ValueError, OSError):
            return None

    def matches_sqlite_target(self, target: str | Path) -> bool:
        if not self.has_live_wiring():
            return False
        configured = self._canonical_target(self._database_wiring)
        actual = self._canonical_target(target)
        return configured is not None and actual is not None and configured == actual

    def matches_source_ref(self, source_ref: str) -> bool:
        return type(source_ref) is str and source_ref == self._source_wiring

    def verify_from_capability(self, *, receipt: SourceBindingAuthorizationEnvelopeV7, purpose: str, readback: object | None = None, db_now: datetime) -> _VerifiedSourceBindingAuthorization | SourceBindingAuthorizationDenied | SourceBindingAuthorizationUnavailable:
        key = self._registry.get(receipt.key_id)
        if key is None or db_now >= receipt.expires_at or db_now >= receipt.drift_expires_at:
            return SourceBindingAuthorizationDenied("not_found_or_denied")
        payload = receipt.model_dump(mode="json", exclude={"signature"})
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        if not hmac.compare_digest(receipt.signature, hmac.new(key, raw, hashlib.sha256).hexdigest()):
            return SourceBindingAuthorizationDenied("source_binding_receipt_invalid")
        try:
            if not self._resolver(receipt, purpose, db_now):
                return SourceBindingAuthorizationDenied("not_found_or_denied")
        except Exception:
            return SourceBindingAuthorizationUnavailable("source_binding_authority_unavailable")
        return _VerifiedSourceBindingAuthorization(_MINT, receipt.receipt_id, receipt.payload_digest, receipt.expires_at)


def _bootstrap_source_binding_wiring(*, issuer_registry: Mapping[str, bytes], resolver: Callable[[SourceBindingAuthorizationEnvelopeV7, str, datetime], bool], database_wiring: object, source_wiring: object) -> SourceBindingAuthorityWiring:  # pyright: ignore[reportUnusedFunction]
    """Private bootstrap helper; test fixtures may import it under trusted-process rules."""
    return SourceBindingAuthorityWiring(_WIRING_MINT, issuer_registry=issuer_registry, resolver=resolver, database_wiring=database_wiring, source_wiring=source_wiring)
