"""Digest-only values and process-local PKCE material for Central browser OIDC.

This module deliberately has no HTTP, token exchange, cookie, or Registry User
admission logic.  Those adapters may receive opaque browser material briefly;
the values below never retain it in a durable model.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import hmac
import re
import secrets
from threading import Lock
from typing import Final, Protocol
from urllib.parse import urlsplit


_DIGEST = re.compile(r"[0-9a-f]{64}")
_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_TERMINAL_REASON = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_HANDLE_BYTES: Final = 32


class BrowserOidcTransactionStore(Protocol):
    """Digest-only durable transaction port.

    The callback application will own its atomic Registry/Authority/session
    transaction in the next slice; this small port only establishes the durable
    representation used by that application.
    """

    def create_transaction(self, transaction: BrowserOidcTransaction) -> None: ...

    def get_transaction(self, transaction_digest: str) -> BrowserOidcTransaction | None: ...


class BrowserSessionStore(Protocol):
    """Digest-only durable session lookup port."""

    def get_session(self, session_digest: str) -> BrowserSession | None: ...


@dataclass(frozen=True, slots=True)
class BrowserOidcTransaction:
    transaction_digest: str
    provider_digest: str
    redirect_uri_digest: str
    state_digest: str
    nonce_digest: str
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None
    terminal_reason: str | None = None

    def __post_init__(self) -> None:
        _validate_digest(self.transaction_digest)
        _validate_digest(self.provider_digest)
        _validate_digest(self.redirect_uri_digest)
        _validate_digest(self.state_digest)
        _validate_digest(self.nonce_digest)
        _validate_time(self.created_at)
        _validate_time(self.expires_at)
        if self.expires_at <= self.created_at:
            raise ValueError("browser transaction expiry must follow creation")
        if (self.consumed_at is None) != (self.terminal_reason is None):
            raise ValueError("browser transaction terminal state must be complete")
        if self.consumed_at is not None:
            _validate_time(self.consumed_at)
            if self.consumed_at < self.created_at:
                raise ValueError("browser transaction consumption precedes creation")
            _validate_terminal_reason(self.terminal_reason)


@dataclass(frozen=True, slots=True)
class BrowserSession:
    session_digest: str
    registry_user_id: str
    org_id: str
    oidc_identity_binding_digest: str
    csrf_digest: str
    registry_fingerprint: str
    registry_revision: int
    established_at: datetime
    expires_at: datetime
    ended_at: datetime | None = None
    terminal_reason: str | None = None

    def __post_init__(self) -> None:
        _validate_digest(self.session_digest)
        _validate_reference(self.registry_user_id)
        _validate_reference(self.org_id)
        _validate_digest(self.oidc_identity_binding_digest)
        _validate_digest(self.csrf_digest)
        _validate_digest(self.registry_fingerprint)
        if type(self.registry_revision) is not int or self.registry_revision < 0:
            raise ValueError("browser session registry revision required")
        _validate_time(self.established_at)
        _validate_time(self.expires_at)
        if self.expires_at <= self.established_at:
            raise ValueError("browser session expiry must follow establishment")
        if (self.ended_at is None) != (self.terminal_reason is None):
            raise ValueError("browser session terminal state must be complete")
        if self.ended_at is not None:
            _validate_time(self.ended_at)
            if self.ended_at < self.established_at:
                raise ValueError("browser session end precedes establishment")
            _validate_terminal_reason(self.terminal_reason)


@dataclass(frozen=True, slots=True)
class BrowserSessionPrincipal:
    """Current browser principal without a browser handle or OIDC claim."""

    session_digest: str
    registry_user_id: str
    org_id: str

    def __post_init__(self) -> None:
        _validate_digest(self.session_digest)
        _validate_reference(self.registry_user_id)
        _validate_reference(self.org_id)

    @classmethod
    def from_session(cls, session: BrowserSession) -> BrowserSessionPrincipal:
        if type(session) is not BrowserSession:
            raise ValueError("browser session value required")
        return cls(
            session_digest=session.session_digest,
            registry_user_id=session.registry_user_id,
            org_id=session.org_id,
        )


class BrowserPkceVerifierVault:
    """Bounded, one-use, process-local verifier material.

    `reserve` is intentionally a boolean fail-closed operation.  A caller must
    reserve before writing the corresponding durable transaction.  Returning
    the verifier from `consume` removes it first, so a replay cannot recover it.
    """

    def __init__(
        self,
        *,
        capacity: int = 1024,
        ttl: timedelta = timedelta(minutes=10),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        if type(capacity) is not int or capacity < 1 or capacity > 1024:
            raise ValueError("browser PKCE capacity must be bounded")
        if ttl <= timedelta() or ttl > timedelta(hours=1):
            raise ValueError("browser PKCE ttl must be bounded")
        self._capacity = capacity
        self._ttl = ttl
        self._clock = clock
        self._fault = fault_injector or _no_fault
        self._entries: dict[str, tuple[bytearray, datetime]] = {}
        self._lock = Lock()

    def reserve(self, transaction_digest: str, verifier: str, expires_at: datetime) -> bool:
        """Atomically reserve memory. No durable write is performed here."""
        try:
            _validate_digest(transaction_digest)
            _validate_verifier(verifier)
            _validate_time(expires_at)
        except Exception:
            return False
        with self._lock:
            inserted = False
            material: bytearray | None = None
            try:
                self._fault("before-reserve")
                now = self._clock()
                _validate_time(now)
                self._evict_expired_locked(now)
                if (
                    expires_at <= now
                    or expires_at > now + self._ttl
                    or transaction_digest in self._entries
                    or len(self._entries) >= self._capacity
                ):
                    return False
                material = bytearray(verifier.encode("utf-8"))
                self._entries[transaction_digest] = (material, expires_at)
                inserted = True
                self._fault("after-reserve")
                return True
            except Exception:
                if inserted:
                    stored = self._entries.pop(transaction_digest, None)
                    if stored is not None:
                        _zeroize(stored[0])
                    elif material is not None:
                        _zeroize(material)
                return False

    def consume(self, transaction_digest: str) -> str | None:
        """Remove and return one verifier, or fail closed on all vault faults."""
        try:
            _validate_digest(transaction_digest)
            with self._lock:
                self._fault("before-consume")
                now = self._clock()
                _validate_time(now)
                self._evict_expired_locked(now)
                entry = self._entries.pop(transaction_digest, None)
                if entry is None:
                    return None
                material, expires_at = entry
                if expires_at <= now:
                    _zeroize(material)
                    return None
                result = material.decode("utf-8")
                _zeroize(material)
                self._fault("after-consume")
                return result
        except Exception:
            return None

    def clear(self) -> None:
        """Best-effort zeroize all local material, e.g. during API shutdown."""
        with self._lock:
            for material, _expiry in self._entries.values():
                _zeroize(material)
            self._entries.clear()

    def discard(self, transaction_digest: str) -> None:
        """Zeroize one reserved verifier when its durable create cannot proceed."""
        try:
            _validate_digest(transaction_digest)
            with self._lock:
                entry = self._entries.pop(transaction_digest, None)
                if entry is not None:
                    _zeroize(entry[0])
        except Exception:
            return None

    @property
    def pending_count(self) -> int:
        try:
            with self._lock:
                now = self._clock()
                _validate_time(now)
                self._evict_expired_locked(now)
                return len(self._entries)
        except Exception:
            return 0

    @property
    def ttl(self) -> timedelta:
        """Configured maximum lifetime; no verifier material is exposed."""
        return self._ttl

    def _evict_expired_locked(self, now: datetime) -> None:
        expired = tuple(
            digest for digest, (_material, expires_at) in self._entries.items() if expires_at <= now
        )
        for digest in expired:
            material, _expiry = self._entries.pop(digest)
            _zeroize(material)


def new_opaque_browser_handle() -> str:
    """Create an unguessable 256-bit browser-wire handle; never persist it."""
    return secrets.token_urlsafe(_HANDLE_BYTES)


def opaque_browser_handle_digest(handle: str) -> str:
    """Digest adapter-only opaque material for durable lookup."""
    if type(handle) is not str or not handle or len(handle) > 1024:
        raise ValueError("opaque browser handle required")
    return sha256(handle.encode("utf-8")).hexdigest()


def browser_redirect_uri(central_public_origin: str) -> str:
    """Derive the sole OIDC callback URI from an exact HTTPS origin."""
    if not _exact_https_origin(central_public_origin):
        raise ValueError("exact HTTPS Central origin required")
    return central_public_origin + "/api/auth/callback"


def constant_time_digest_matches(raw_value: str, expected_digest: str) -> bool:
    """Compare adapter input against a durable digest without exposing a branch."""
    try:
        _validate_digest(expected_digest)
        actual = opaque_browser_handle_digest(raw_value)
    except ValueError:
        return False
    return hmac.compare_digest(actual, expected_digest)


def _validate_digest(value: object) -> None:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ValueError("canonical SHA-256 digest required")


def _validate_reference(value: object) -> None:
    if type(value) is not str or _REFERENCE.fullmatch(value) is None:
        raise ValueError("browser reference required")


def _validate_time(value: object) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware browser timestamp required")


def _validate_terminal_reason(value: object) -> None:
    if type(value) is not str or _TERMINAL_REASON.fullmatch(value) is None:
        raise ValueError("browser terminal reason required")


def _validate_verifier(value: object) -> None:
    if type(value) is not str or not value or len(value) > 512:
        raise ValueError("PKCE verifier required")
    try:
        value.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError("PKCE verifier must be ASCII") from error


def _exact_https_origin(value: object) -> bool:
    if type(value) is not str or any(ord(character) < 32 for character in value):
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and bool(parsed.netloc)
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
        and (port is None or port != 443)
    )


def _zeroize(value: bytearray) -> None:
    for index in range(len(value)):
        value[index] = 0


def _no_fault(_point: str) -> None:
    return None


__all__ = [
    "BrowserOidcTransaction",
    "BrowserOidcTransactionStore",
    "BrowserPkceVerifierVault",
    "BrowserSession",
    "BrowserSessionPrincipal",
    "BrowserSessionStore",
    "browser_redirect_uri",
    "constant_time_digest_matches",
    "new_opaque_browser_handle",
    "opaque_browser_handle_digest",
]
