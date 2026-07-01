"""공급자 transport 일시 장애 재시도 정책 (ADR 0027 `ClaudeApiRuntime`·`AnthropicSdkTransport`).

결함(수정 대상): `provider_runtime.py`·`provider_transport_anthropic.py`에 429(레이트리밋)/
503(과부하)/일시 네트워크 오류 처리가 전무했다. 어떤 예외든 즉시 상위(worker·dispatch·web)의
escalation/폴백으로 승격돼, 재시도 한 번이면 살릴 답이 불필요하게 사람에게 넘어갔다.

설계(결정론 테스트 가능):
  - **판정은 순수 함수** — `is_retryable`·`is_auth_error`가 예외의 `status_code` 속성/클래스
    이름만 보고 재시도 가능 여부를 정한다. anthropic SDK는 선택 extra라 이 모듈은 SDK를
    import하지 않는다(게이트 경계 패턴 — 판정은 상태코드/속성 기반이라 SDK 없이도 테스트된다).
  - **정책은 frozen 값 객체** — `RetryPolicy(max_attempts, backoff_base_s)` + `backoff_delays()`
    순수 함수(지수 백오프 시퀀스 계산 — sleep 없이 값만).
  - **재시도 루프는 sleep 주입 seam** — `run_with_retry(fn, policy, sleeper=...)`. 테스트는 fake
    sleeper 주입으로 즉시 진행(실 sleep 0·백오프 순서를 결정론 단언).

불변식 보존:
  - 401/403(owner 토큰 만료·갱신 필요)은 **재시도하지 않고** `ProviderAuthError`로 승격 —
    호출자가 "미아"(일시 장애)와 "토큰 갱신 필요"(항구적·사람 개입)를 구분할 수 있게 한다.
  - 최종 실패(재시도 소진)는 **원 예외를 그대로 재던진다** — 기존 escalation 흐름·"미아 없음"
    폴백(worker `handle_push_work`·`ClaudeCodeRuntime` 흡수)을 절대 깨지 않는다.
"""

from __future__ import annotations

from collections.abc import Callable
from time import sleep as _real_sleep
from typing import TypeVar

# ── 재시도 seam 타입 ─────────────────────────────────────────────────────
#
# Sleeper: 백오프 대기의 주입 seam(초 단위). 테스트는 no-op fake를 주입해 실 sleep 0으로
# 즉시 진행하되 *호출된 지연 시퀀스*를 기록해 백오프 순서를 결정론 단언한다. 프로덕션 기본은
# `time.sleep`(실 대기). Notifier·GitGateway·ClaudeRunner 주입과 동형(비결정·IO를 주입으로 격리).

Sleeper = Callable[[float], None]

T = TypeVar("T")


# ── 인증 실패 승격 예외: ProviderAuthError ───────────────────────────────
#
# 401/403은 일시 장애가 아니라 owner 자격의 항구적 결함(토큰 만료·갱신 필요·권한 없음)이다.
# 재시도로 살릴 수 없고 사람 개입(토큰 재발급)이 필요하다. 그래서 다른 일시 장애와 *구분
# 가능한 예외*로 승격해 호출자가 "미아"(재시도 소진)와 "토큰 갱신 필요"를 갈라볼 수 있게 한다.
# 원 SDK 예외를 __cause__로 보존한다(진단·로그용).


class ProviderAuthError(Exception):
    """공급자 API 인증/인가 실패(401/403) — 재시도 불가·owner 토큰 갱신 필요.

    일시 장애(429/5xx/네트워크)와 달리 재시도로 살릴 수 없다. `run_with_retry`가 이 부류를
    감지하면 재시도하지 않고 즉시 이 예외로 승격한다 — 호출자가 escalation 사유를 "일시 장애"가
    아니라 "owner 자격 갱신 필요"로 구분할 수 있게. 원 예외는 `raise ... from exc`로 보존한다.
    """


# ── 판정 순수 함수 (SDK import 0 — 상태코드/속성/클래스 이름 기반) ─────────


def _status_code(exc: BaseException) -> int | None:
    """예외에서 HTTP 상태코드를 뽑는다(anthropic `APIStatusError.status_code` 규약).

    anthropic SDK의 `APIStatusError`(및 서브클래스 RateLimitError·OverloadedError·
    AuthenticationError 등)는 `status_code: int` 인스턴스 속성을 갖는다. SDK를 import하지 않고
    duck-typing으로 읽어 SDK 미설치 환경에서도(가짜 예외로) 테스트된다. 속성이 없거나 int가
    아니면 None(상태코드 없는 예외 — 연결/타임아웃 부류).
    """
    code = getattr(exc, "status_code", None)
    if isinstance(code, bool):  # bool은 int 서브타입 — 상태코드로 취급하지 않는다.
        return None
    if isinstance(code, int):
        return code
    return None


# 연결/타임아웃 부류로 볼 예외 클래스 이름(SDK import 없이 이름 기반 판정).
#   - anthropic: APIConnectionError·APITimeoutError (status_code 없음)
#   - stdlib: ConnectionError(및 서브클래스)·TimeoutError·socket.timeout
_TRANSIENT_NETWORK_EXC_NAMES = frozenset(
    {
        "APIConnectionError",
        "APITimeoutError",
        "ConnectionError",
        "ConnectionResetError",
        "ConnectionAbortedError",
        "ConnectionRefusedError",
        "TimeoutError",
        "timeout",  # socket.timeout 별칭(구 Python)
    }
)


def _is_network_transient(exc: BaseException) -> bool:
    """연결 오류/타임아웃 등 상태코드 없는 일시 네트워크 실패인가.

    stdlib 표준 예외는 isinstance로(하위호환·명시), SDK 예외(APIConnectionError·APITimeoutError)는
    SDK를 import하지 않으려 클래스 이름(MRO 전체)으로 판정한다. 이름 기반이라 가짜 예외로도 테스트된다.
    """
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    names = {cls.__name__ for cls in type(exc).__mro__}
    return bool(names & _TRANSIENT_NETWORK_EXC_NAMES)


def is_auth_error(exc: BaseException) -> bool:
    """401/403 — owner 토큰 만료·갱신 필요·권한 없음(재시도 불가·사람 개입).

    재시도 X. `run_with_retry`가 이 판정이 참이면 즉시 `ProviderAuthError`로 승격한다.
    """
    return _status_code(exc) in (401, 403)


def is_retryable(exc: BaseException) -> bool:
    """이 예외가 재시도로 살릴 수 있는 *일시* 장애인가(순수 판정 — SDK import 0).

    재시도 O:
      - 429 rate limit(레이트리밋)
      - 5xx(500·503 등 — 서버 과부하·일시 장애)
      - 연결 오류/타임아웃(상태코드 없는 네트워크 일시 실패)
    재시도 X:
      - 401/403(인증 — owner 토큰 만료·갱신 필요) → `is_auth_error`가 별도로 승격
      - 400(요청 자체 결함 — 재시도해도 같은 실패)
      - 그 외 4xx(404·409·413·422 등 비일시 요청 오류)
      - 상태코드도 네트워크 부류도 아닌 예외(프로그래밍 오류 등 — 재시도 무의미)
    """
    code = _status_code(exc)
    if code is not None:
        if code in (401, 403):
            return False  # 인증 — 재시도 아님(is_auth_error가 승격).
        if code == 429:
            return True  # rate limit.
        if 500 <= code < 600:
            return True  # 서버 과부하·일시.
        return False  # 400·404·409·413·422 등 비일시 요청 오류.
    # 상태코드 없음 — 연결/타임아웃 부류만 재시도.
    return _is_network_transient(exc)


# ── 재시도 정책 값 객체: RetryPolicy ─────────────────────────────────────
#
# 재시도 횟수·백오프를 담는 frozen 값 객체. 정책 계산(백오프 지연 시퀀스)은 순수 함수로 분리해
# sleep 없이 값만 단언한다(결정론). RetryPolicy 자체는 값이라 IO·주입을 모른다 — 루프가
# sleeper를 주입받아 이 값 객체가 낸 지연을 실 대기로 바꾼다(값/부작용 분리).


class RetryPolicy:
    """일시 장애 재시도 정책(frozen 값 객체 — 지수 백오프).

    - `max_attempts`: 총 시도 횟수(최초 1회 포함). 1이면 재시도 없음(기존 동작). 기본 3.
    - `backoff_base_s`: 첫 재시도 전 대기(초). 지수 배가 — n번째 재시도 전 대기는
      `backoff_base_s * (backoff_factor ** (n-1))`.
    - `backoff_factor`: 지수 배가 비율(기본 2.0).
    - `max_backoff_s`: 개별 대기 상한(초) — 폭주 방지(기본 30.0).

    `backoff_delays()`는 각 재시도 전 대기 시퀀스(길이 `max_attempts - 1`)를 순수 계산한다.
    실 sleep은 `run_with_retry`가 주입 sleeper로 수행한다(값/부작용 분리).
    """

    __slots__ = ("max_attempts", "backoff_base_s", "backoff_factor", "max_backoff_s")

    max_attempts: int
    backoff_base_s: float
    backoff_factor: float
    max_backoff_s: float

    def __init__(
        self,
        max_attempts: int = 3,
        backoff_base_s: float = 0.5,
        *,
        backoff_factor: float = 2.0,
        max_backoff_s: float = 30.0,
    ) -> None:
        if max_attempts < 1:
            raise ValueError(f"max_attempts는 1 이상이어야 한다(기존 동작=1) — 받은 값: {max_attempts}")
        if backoff_base_s < 0:
            raise ValueError(f"backoff_base_s는 음수일 수 없다 — 받은 값: {backoff_base_s}")
        if backoff_factor < 1:
            raise ValueError(f"backoff_factor는 1 이상이어야 한다 — 받은 값: {backoff_factor}")
        if max_backoff_s < 0:
            raise ValueError(f"max_backoff_s는 음수일 수 없다 — 받은 값: {max_backoff_s}")
        object.__setattr__(self, "max_attempts", max_attempts)
        object.__setattr__(self, "backoff_base_s", backoff_base_s)
        object.__setattr__(self, "backoff_factor", backoff_factor)
        object.__setattr__(self, "max_backoff_s", max_backoff_s)

    def __setattr__(self, name: str, value: object) -> None:  # frozen
        raise AttributeError(f"RetryPolicy는 frozen이다 — {name} 변경 불가")

    def __repr__(self) -> str:
        return (
            f"RetryPolicy(max_attempts={self.max_attempts}, "
            f"backoff_base_s={self.backoff_base_s}, backoff_factor={self.backoff_factor}, "
            f"max_backoff_s={self.max_backoff_s})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RetryPolicy):
            return NotImplemented
        return (
            self.max_attempts == other.max_attempts
            and self.backoff_base_s == other.backoff_base_s
            and self.backoff_factor == other.backoff_factor
            and self.max_backoff_s == other.max_backoff_s
        )

    def __hash__(self) -> int:
        return hash(
            (self.max_attempts, self.backoff_base_s, self.backoff_factor, self.max_backoff_s)
        )

    def backoff_delays(self) -> tuple[float, ...]:
        """각 재시도 *직전* 대기 시퀀스(초) — 순수 계산(sleep 0).

        길이는 `max_attempts - 1`(재시도 횟수). n번째(1-기반) 재시도 전 대기는
        `min(backoff_base_s * backoff_factor**(n-1), max_backoff_s)`. max_attempts=1이면
        빈 튜플(재시도 없음). 지수 백오프 — 첫 재시도 base, 다음 base*factor, …(상한 clamp).
        """
        delays: list[float] = []
        for n in range(self.max_attempts - 1):
            raw = self.backoff_base_s * (self.backoff_factor**n)
            delays.append(min(raw, self.max_backoff_s))
        return tuple(delays)


# 프로덕션 기본 정책 — 3회 시도·0.5초 base 지수 백오프. 워커/web가 별 정책을 주입하지 않으면 이 값.
DEFAULT_RETRY_POLICY = RetryPolicy()

# 재시도 없음(기존 동작 그대로) — 무회귀 검증·재시도 비활성 주입용.
NO_RETRY_POLICY = RetryPolicy(max_attempts=1)


# ── 재시도 루프: run_with_retry ──────────────────────────────────────────


def run_with_retry(
    fn: Callable[[], T],
    *,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    sleeper: Sleeper = _real_sleep,
    is_retryable_fn: Callable[[BaseException], bool] = is_retryable,
) -> T:
    """`fn`을 정책대로 재시도하며 호출한다(sleep 주입 seam·지수 백오프).

    흐름(시도 n = 1..max_attempts):
      1. `fn()` 성공 → 결과 반환.
      2. 인증 실패(`is_auth_error`) → 재시도 없이 즉시 `ProviderAuthError`로 승격(원 예외 __cause__).
      3. 재시도 가능(`is_retryable_fn`) & 시도 남음 → `sleeper(delay)` 대기 후 재시도(지수 백오프).
      4. 재시도 불가 또는 시도 소진 → **원 예외를 그대로 재던진다**(기존 escalation·폴백 경로 보존).

    `sleeper` 주입으로 테스트는 실 sleep 0·백오프 순서를 결정론 단언한다(fake sleeper가 지연 기록).
    `policy.backoff_delays()`가 재시도 직전 대기 시퀀스를 순수 계산한다(값/부작용 분리).
    """
    delays = policy.backoff_delays()
    last_exc: BaseException | None = None
    for attempt in range(policy.max_attempts):
        try:
            return fn()
        except BaseException as exc:  # noqa: BLE001 — 판정 함수가 재시도/승격/재던짐을 가른다.
            if is_auth_error(exc):
                # 401/403 — 재시도 없이 즉시 승격(owner 토큰 갱신 필요). 원 예외 보존.
                raise ProviderAuthError(
                    "공급자 인증/인가 실패(401/403) — owner 토큰 만료·갱신 필요"
                ) from exc
            last_exc = exc
            has_next = attempt < policy.max_attempts - 1
            if not (has_next and is_retryable_fn(exc)):
                # 재시도 불가(400·기타) 또는 시도 소진 — 원 예외 그대로 재던짐(escalation 경로 보존).
                raise
            sleeper(delays[attempt])
    # 도달 불가(루프가 반환하거나 재던진다) — 방어적. last_exc가 있으면 재던짐.
    if last_exc is not None:  # pragma: no cover
        raise last_exc
    raise RuntimeError("run_with_retry: 시도 없이 종료(max_attempts 검증 위반)")  # pragma: no cover
