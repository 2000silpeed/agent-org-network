"""공급자 transport 일시 장애 재시도 정책 (provider_retry) [게이트 내·결정론]

ADR 0027 `ClaudeApiRuntime`·`AnthropicSdkTransport`의 429/5xx/네트워크 일시 실패 재시도.

불변식(단언 대상):
- 판정 순수 함수: 429/5xx/네트워크 재시도 O, 401/403/400/기타 재시도 X (SDK import 0·가짜 예외)
- 401/403 즉시 ProviderAuthError 승격(재시도 0·원 예외 __cause__ 보존)
- 재시도 소진 후 원 예외 그대로 재던짐(escalation·"미아 없음" 폴백 보존)
- sleep 주입 seam: fake sleeper로 실 sleep 0·지수 백오프 순서 결정론
- RetryPolicy frozen 값 객체·backoff_delays 순수 계산
"""

from __future__ import annotations

import pytest

from agent_org_network.provider_retry import (
    DEFAULT_RETRY_POLICY,
    NO_RETRY_POLICY,
    ProviderAuthError,
    RetryPolicy,
    is_auth_error,
    is_retryable,
    run_with_retry,
)


# ---------------------------------------------------------------------------
# 가짜 예외 — SDK 없이도 판정 테스트(상태코드 속성/클래스 이름 기반)
# ---------------------------------------------------------------------------


class FakeStatusError(Exception):
    """anthropic `APIStatusError`의 status_code 규약을 흉내내는 가짜(SDK import 0)."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


# 판정이 클래스 *이름*으로 네트워크 부류를 알아보므로(SDK import 0), 실제 anthropic
# 예외명과 정확히 같은 이름을 쓴다(SDK 없이 이름 매칭 검증 — 실 SDK 예외도 같은 이름).
class APIConnectionError(Exception):
    """anthropic `APIConnectionError`(status_code 없음)를 이름으로 흉내낸다."""


class APITimeoutError(Exception):
    """anthropic `APITimeoutError`(status_code 없음)를 이름으로 흉내낸다."""


class _FakeSleeper:
    """호출된 지연 시퀀스를 기록하는 no-op sleeper — 실 sleep 0·백오프 순서 단언용."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


# ---------------------------------------------------------------------------
# is_retryable — 순수 판정(재시도 O)
# ---------------------------------------------------------------------------


class TestIsRetryableTrue:
    def test_429_rate_limit은_재시도(self) -> None:
        assert is_retryable(FakeStatusError(429)) is True

    def test_500_서버오류는_재시도(self) -> None:
        assert is_retryable(FakeStatusError(500)) is True

    def test_503_과부하는_재시도(self) -> None:
        assert is_retryable(FakeStatusError(503)) is True

    def test_502_504도_재시도(self) -> None:
        assert is_retryable(FakeStatusError(502)) is True
        assert is_retryable(FakeStatusError(504)) is True

    def test_연결오류_이름은_재시도(self) -> None:
        assert is_retryable(APIConnectionError()) is True

    def test_타임아웃_이름은_재시도(self) -> None:
        assert is_retryable(APITimeoutError()) is True

    def test_stdlib_ConnectionError는_재시도(self) -> None:
        assert is_retryable(ConnectionError("reset")) is True

    def test_stdlib_TimeoutError는_재시도(self) -> None:
        assert is_retryable(TimeoutError("timed out")) is True


# ---------------------------------------------------------------------------
# is_retryable — 순수 판정(재시도 X)
# ---------------------------------------------------------------------------


class TestIsRetryableFalse:
    def test_401_인증은_재시도_아님(self) -> None:
        assert is_retryable(FakeStatusError(401)) is False

    def test_403_인가는_재시도_아님(self) -> None:
        assert is_retryable(FakeStatusError(403)) is False

    def test_400_요청결함은_재시도_아님(self) -> None:
        assert is_retryable(FakeStatusError(400)) is False

    def test_404_409_413_422는_재시도_아님(self) -> None:
        for code in (404, 409, 413, 422):
            assert is_retryable(FakeStatusError(code)) is False

    def test_상태코드도_네트워크도_아닌_예외는_재시도_아님(self) -> None:
        assert is_retryable(ValueError("프로그래밍 오류")) is False

    def test_bool_status_code는_상태코드로_취급_안함(self) -> None:
        # getattr가 True(=1 서브타입)를 status_code로 오인해선 안 된다 — bool은 걸러낸다.
        exc = Exception()
        exc.status_code = True  # type: ignore[attr-defined]
        assert is_retryable(exc) is False


# ---------------------------------------------------------------------------
# is_auth_error — 401/403 판정
# ---------------------------------------------------------------------------


class TestIsAuthError:
    def test_401은_auth_error(self) -> None:
        assert is_auth_error(FakeStatusError(401)) is True

    def test_403은_auth_error(self) -> None:
        assert is_auth_error(FakeStatusError(403)) is True

    def test_429는_auth_error_아님(self) -> None:
        assert is_auth_error(FakeStatusError(429)) is False

    def test_500은_auth_error_아님(self) -> None:
        assert is_auth_error(FakeStatusError(500)) is False

    def test_상태코드_없는_예외는_auth_error_아님(self) -> None:
        assert is_auth_error(APIConnectionError()) is False


# ---------------------------------------------------------------------------
# RetryPolicy — frozen 값 객체 + backoff_delays 순수 계산
# ---------------------------------------------------------------------------


class TestRetryPolicy:
    def test_기본값(self) -> None:
        p = RetryPolicy()
        assert p.max_attempts == 3
        assert p.backoff_base_s == 0.5
        assert p.backoff_factor == 2.0

    def test_frozen이다(self) -> None:
        p = RetryPolicy()
        with pytest.raises(AttributeError):
            p.max_attempts = 5  # type: ignore[misc]

    def test_equality와_hash(self) -> None:
        assert RetryPolicy(3, 0.5) == RetryPolicy(3, 0.5)
        assert hash(RetryPolicy(3, 0.5)) == hash(RetryPolicy(3, 0.5))
        assert RetryPolicy(3, 0.5) != RetryPolicy(2, 0.5)

    def test_backoff_delays_지수_시퀀스(self) -> None:
        # max_attempts=3 → 재시도 2회 → 대기 2개: base, base*factor.
        p = RetryPolicy(max_attempts=3, backoff_base_s=0.5, backoff_factor=2.0)
        assert p.backoff_delays() == (0.5, 1.0)

    def test_backoff_delays_길이는_재시도_횟수(self) -> None:
        p = RetryPolicy(max_attempts=4, backoff_base_s=1.0, backoff_factor=3.0)
        assert p.backoff_delays() == (1.0, 3.0, 9.0)

    def test_backoff_delays_상한_clamp(self) -> None:
        p = RetryPolicy(max_attempts=5, backoff_base_s=10.0, backoff_factor=2.0, max_backoff_s=30.0)
        # 10, 20, 40→30(clamp), 80→30(clamp)
        assert p.backoff_delays() == (10.0, 20.0, 30.0, 30.0)

    def test_max_attempts_1은_재시도_없음(self) -> None:
        assert RetryPolicy(max_attempts=1).backoff_delays() == ()

    def test_max_attempts_0_이하는_거부(self) -> None:
        with pytest.raises(ValueError):
            RetryPolicy(max_attempts=0)

    def test_음수_backoff_거부(self) -> None:
        with pytest.raises(ValueError):
            RetryPolicy(backoff_base_s=-1.0)

    def test_factor_1_미만_거부(self) -> None:
        with pytest.raises(ValueError):
            RetryPolicy(backoff_factor=0.5)


# ---------------------------------------------------------------------------
# run_with_retry — 성공 경로
# ---------------------------------------------------------------------------


class TestRunWithRetrySuccess:
    def test_첫_시도_성공은_재시도_없음(self) -> None:
        sleeper = _FakeSleeper()
        calls = {"n": 0}

        def fn() -> str:
            calls["n"] += 1
            return "성공"

        result = run_with_retry(fn, policy=DEFAULT_RETRY_POLICY, sleeper=sleeper)
        assert result == "성공"
        assert calls["n"] == 1
        assert sleeper.calls == []  # sleep 0

    def test_일시_실패_후_성공하면_재시도로_복구(self) -> None:
        sleeper = _FakeSleeper()
        attempts = {"n": 0}

        def fn() -> str:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise FakeStatusError(503)  # 과부하 — 재시도 가능
            return "복구됨"

        result = run_with_retry(
            fn, policy=RetryPolicy(max_attempts=3, backoff_base_s=0.5), sleeper=sleeper
        )
        assert result == "복구됨"
        assert attempts["n"] == 3


# ---------------------------------------------------------------------------
# run_with_retry — 백오프 순서(결정론·실 sleep 0)
# ---------------------------------------------------------------------------


class TestRunWithRetryBackoff:
    def test_백오프_지연_순서가_지수적이다(self) -> None:
        sleeper = _FakeSleeper()

        def fn() -> str:
            raise FakeStatusError(429)  # 항상 실패 → 소진까지 재시도

        with pytest.raises(FakeStatusError):
            run_with_retry(
                fn,
                policy=RetryPolicy(max_attempts=3, backoff_base_s=0.5, backoff_factor=2.0),
                sleeper=sleeper,
            )
        # 재시도 2회 → 대기 2번: 0.5, 1.0(지수).
        assert sleeper.calls == [0.5, 1.0]

    def test_재시도_횟수만큼_시도한다(self) -> None:
        sleeper = _FakeSleeper()
        attempts = {"n": 0}

        def fn() -> str:
            attempts["n"] += 1
            raise FakeStatusError(500)

        with pytest.raises(FakeStatusError):
            run_with_retry(
                fn, policy=RetryPolicy(max_attempts=4, backoff_base_s=0.1), sleeper=sleeper
            )
        assert attempts["n"] == 4  # 최초 1 + 재시도 3
        assert len(sleeper.calls) == 3  # 재시도 직전 대기 3번


# ---------------------------------------------------------------------------
# run_with_retry — 401/403 즉시 승격
# ---------------------------------------------------------------------------


class TestRunWithRetryAuthEscalation:
    def test_401은_재시도_없이_ProviderAuthError로_승격(self) -> None:
        sleeper = _FakeSleeper()
        attempts = {"n": 0}

        def fn() -> str:
            attempts["n"] += 1
            raise FakeStatusError(401)

        with pytest.raises(ProviderAuthError):
            run_with_retry(fn, policy=DEFAULT_RETRY_POLICY, sleeper=sleeper)
        assert attempts["n"] == 1  # 재시도 없음
        assert sleeper.calls == []  # sleep 0

    def test_403도_ProviderAuthError로_승격(self) -> None:
        def fn() -> str:
            raise FakeStatusError(403)

        with pytest.raises(ProviderAuthError):
            run_with_retry(fn, policy=DEFAULT_RETRY_POLICY, sleeper=_FakeSleeper())

    def test_ProviderAuthError는_원_예외를_cause로_보존한다(self) -> None:
        original = FakeStatusError(401)

        def fn() -> str:
            raise original

        with pytest.raises(ProviderAuthError) as excinfo:
            run_with_retry(fn, policy=DEFAULT_RETRY_POLICY, sleeper=_FakeSleeper())
        assert excinfo.value.__cause__ is original


# ---------------------------------------------------------------------------
# run_with_retry — 재시도 소진/비일시 → 원 예외 재던짐(escalation 보존)
# ---------------------------------------------------------------------------


class TestRunWithRetryReRaise:
    def test_재시도_소진_후_원_예외_그대로_재던짐(self) -> None:
        def fn() -> str:
            raise FakeStatusError(503)

        with pytest.raises(FakeStatusError) as excinfo:
            run_with_retry(
                fn, policy=RetryPolicy(max_attempts=2, backoff_base_s=0.1), sleeper=_FakeSleeper()
            )
        assert excinfo.value.status_code == 503  # ProviderAuthError로 뭉개지 않음

    def test_400_비일시는_재시도_없이_즉시_재던짐(self) -> None:
        sleeper = _FakeSleeper()
        attempts = {"n": 0}

        def fn() -> str:
            attempts["n"] += 1
            raise FakeStatusError(400)

        with pytest.raises(FakeStatusError):
            run_with_retry(fn, policy=DEFAULT_RETRY_POLICY, sleeper=sleeper)
        assert attempts["n"] == 1  # 재시도 없음
        assert sleeper.calls == []

    def test_비재시도_예외는_그대로_전파(self) -> None:
        def fn() -> str:
            raise ValueError("프로그래밍 오류")

        with pytest.raises(ValueError):
            run_with_retry(fn, policy=DEFAULT_RETRY_POLICY, sleeper=_FakeSleeper())

    def test_NO_RETRY_POLICY는_재시도하지_않는다(self) -> None:
        sleeper = _FakeSleeper()
        attempts = {"n": 0}

        def fn() -> str:
            attempts["n"] += 1
            raise FakeStatusError(503)

        with pytest.raises(FakeStatusError):
            run_with_retry(fn, policy=NO_RETRY_POLICY, sleeper=sleeper)
        assert attempts["n"] == 1  # max_attempts=1 → 재시도 없음
        assert sleeper.calls == []
