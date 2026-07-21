"""Question Request 상관키의 작은 공통 검증 규율(P17.2c-1a).

기존 값과 durable 행은 ``request_id=None``을 계속 허용한다. 새 Request-first 경로가
상관키를 싣는 순간에는 nonblank 문자열이어야 하며, WorkTicket은 실행 시도와 항상
한 쌍으로 존재해야 한다. 이 모듈은 ID를 정규화하거나 legacy 값을 추정하지 않는다.
"""

from __future__ import annotations


class LinkedEntityMismatchError(ValueError):
    """같은 Request에 이미 연결된 객체와 재시도 payload가 다른 경우."""


def require_request_id(request_id: object) -> str:
    """새 Request-aware 생성 관문에서 nonblank 상관키를 강제한다."""
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError("request_id는 nonblank 문자열이어야 합니다.")
    return request_id


def validate_optional_request_id(request_id: object) -> str | None:
    """legacy ``None``은 보존하고, 값이 있으면 nonblank인지 확인한다."""
    if request_id is None:
        return None
    return require_request_id(request_id)


def require_positive_attempt(attempt: object) -> int:
    """Request-aware WorkTicket 실행 시도를 strict 양의 정수로 제한한다."""
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ValueError("WorkTicket attempt는 1 이상의 정수여야 합니다.")
    return attempt


def validate_ticket_correlation(
    request_id: object,
    attempt: object,
) -> tuple[str | None, int | None]:
    """WorkTicket의 ``request_id``와 ``attempt`` 쌍 및 양의 시도를 검증한다."""
    if request_id is None and attempt is None:
        return None, None
    if request_id is None or attempt is None:
        raise ValueError("WorkTicket request_id와 attempt는 함께 있어야 합니다.")
    correlated_request_id = require_request_id(request_id)
    return correlated_request_id, require_positive_attempt(attempt)
