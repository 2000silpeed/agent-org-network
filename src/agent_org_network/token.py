"""워커 admission 토큰 — TokenStore 포트 + AdmissionToken + InMemoryTokenStore (T9.5(a), ADR 0026).

기존 store 패턴(BackupReviewStore·SessionStore·ConflictCaseStore)의 N번째 인스턴스 — 새 메커니즘 0.

불변식:
  - 평문 미저장: 발급 시 raw_token을 1회만 반환하고 store엔 해시만 보관(DB 유출 시 평문 미노출).
  - 등록 무결성: 유효하지 않은 토큰(만료·revoke·위조·없음)은 verify None → admission 거부.
  - Authority 중앙: 토큰은 중앙 발급·owner 귀속 선언이지 워커 자기보고 아님.
  - owner 격리: 토큰의 owner_id 귀속이 회신 출처를 그 owner로 강제.
  - 전이≠기록: 발급/revoke는 도메인 admission 상태이지 audit 로그 아님.
  - append-only revoke: revoked=True 표식(삭제 X) — Precedent.invalidated 패턴.
  - clock 진전 시 만료: verify 시 now 파라미터로 주입 결정론.

결정론 seam(ADR 0026 결정 4·결정 1):
  - token_factory 주입: 기본 secrets.token_urlsafe(비결정)이나 테스트에선 결정론 팩토리 주입.
  - 해시: hashlib.sha256(결정론).
  - 만료 판정: verify의 now 파라미터 주입(주입 clock — SessionStore·ConflictCase와 동형).
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Protocol

WorkerRole = Literal["primary", "backup"]


def _default_token_factory() -> str:
    return secrets.token_urlsafe(32)


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _new_token_id() -> str:
    return uuid.uuid4().hex


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class AdmissionToken:
    """워커 admission 토큰 값 객체(ADR 0026 결정 1).

    token_id: 안정 식별자(콘솔 revoke 대상·로그).
    owner_id: 이 토큰이 귀속된 owner(중앙 발급·귀속 선언이지 워커 자기보고 아님).
    role: primary | backup(ADR 0012 — 그 owner 안 우선순위).
    token_hash: 저장은 해시(평문은 발급 시 1회만 반환·DB 유출 시 평문 미노출).
    issued_at: 발급 시각.
    expires_at: 만료 시각(None이면 만료 없음).
    revoked: append-only 표식(삭제 X) — Precedent.invalidated 패턴.
    revoked_at: revoke 시각(revoked=True일 때 설정).
    """

    token_id: str
    owner_id: str
    role: WorkerRole
    token_hash: str
    issued_at: datetime
    expires_at: datetime | None
    revoked: bool = False
    revoked_at: datetime | None = None


class TokenStore(Protocol):
    """등록 토큰의 발급·검증·만료·revoke 포트 (ADR 0026 결정 1).

    BackupReviewStore·SessionStore·ConflictCaseStore와 같은 포트 패턴
    (Protocol + InMemoryTokenStore + 후속 SqliteTokenStore).
    """

    def issue(
        self,
        owner_id: str,
        role: WorkerRole,
        *,
        now: datetime,
        expires_in: timedelta | None = None,
    ) -> tuple[str, AdmissionToken]:
        """운영자가 (owner/role)용 등록 토큰을 발급한다.

        평문 raw_token을 그 자리에서 1회만 반환하고, store엔 해시만 보관.
        owner 귀속을 토큰에 박는다(워커 자기보고가 아니라 중앙 발급 — Authority 중앙).
        """
        ...

    def verify(self, raw_token: str, *, now: datetime) -> AdmissionToken | None:
        """워커 토큰을 검증한다 — 미만료·미revoke·해시 일치면 AdmissionToken, 아니면 None."""
        ...

    def revoke(self, token_id: str) -> AdmissionToken | None:
        """토큰을 취소한다 — append-only(삭제 X·revoked=True 표식).

        revoke된 토큰은 verify에서 None. 멱등(이미 revoked면 그대로 반환).
        없으면 None.
        """
        ...

    def list_active(self, now: datetime | None = None) -> list[AdmissionToken]:
        """콘솔이 '연결/대기 워커' 목록을 그리는 원천 — 미만료·미revoke 토큰."""
        ...


class InMemoryTokenStore:
    """in-memory TokenStore 구현 — 색인: token_hash(verify)·token_id(revoke).

    BackupReviewStore·SessionStore와 같은 포트+InMemory 구조.
    append-only revoke: 삭제가 아니라 revoked=True 표식(Precedent.invalidate 패턴).
    """

    def __init__(
        self,
        token_factory: Callable[[], str] = _default_token_factory,
        clock: Callable[[], datetime] = _default_clock,
    ) -> None:
        self._token_factory = token_factory
        self._clock = clock
        # token_hash → AdmissionToken(verify 색인 — O(1) 조회)
        self._by_hash: dict[str, AdmissionToken] = {}
        # token_id → AdmissionToken(revoke 색인 — O(1) 조회)
        self._by_id: dict[str, AdmissionToken] = {}

    def issue(
        self,
        owner_id: str,
        role: WorkerRole,
        *,
        now: datetime,
        expires_in: timedelta | None = None,
    ) -> tuple[str, AdmissionToken]:
        raw = self._token_factory()
        token_hash = _hash_token(raw)
        token_id = _new_token_id()
        expires_at = (now + expires_in) if expires_in is not None else None

        token = AdmissionToken(
            token_id=token_id,
            owner_id=owner_id,
            role=role,
            token_hash=token_hash,
            issued_at=now,
            expires_at=expires_at,
        )
        self._by_hash[token_hash] = token
        self._by_id[token_id] = token
        return raw, token

    def verify(self, raw_token: str, *, now: datetime) -> AdmissionToken | None:
        """해시 색인으로 조회 후 유효성 검사 — 미만료·미revoke이면 반환."""
        token_hash = _hash_token(raw_token)
        token = self._by_hash.get(token_hash)
        if token is None:
            return None
        if token.revoked:
            return None
        if token.expires_at is not None and now >= token.expires_at:
            return None
        return token

    def revoke(self, token_id: str) -> AdmissionToken | None:
        """append-only revoke — 삭제 X·revoked=True 표식·멱등."""
        token = self._by_id.get(token_id)
        if token is None:
            return None
        if token.revoked:
            return token

        import dataclasses

        revoked = dataclasses.replace(
            token,
            revoked=True,
            revoked_at=self._clock(),
        )
        self._by_hash[token.token_hash] = revoked
        self._by_id[token_id] = revoked
        return revoked

    def list_active(self, now: datetime | None = None) -> list[AdmissionToken]:
        """미revoke + 미만료 토큰 목록 — 콘솔 워커 현황 원천."""
        effective_now = now if now is not None else self._clock()
        result: list[AdmissionToken] = []
        for token in self._by_id.values():
            if token.revoked:
                continue
            if token.expires_at is not None and effective_now >= token.expires_at:
                continue
            result.append(token)
        return result
