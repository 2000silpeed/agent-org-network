"""T9.6 — 실 anthropic SDK ProviderTransport (ADR 0027 결정 2·4·9·10) [게이트 밖]

`StubProviderTransport`(게이트 기본·결정론)와 *같은 Protocol*(`ProviderTransport`)을 만족하는
**실 공급자 transport**. owner OAuth 프로필 위임 + 공식 anthropic SDK 인프로세스 스트리밍.

게이트 경계(정직한 분리):
  - 이 모듈은 `anthropic` SDK를 *모듈 상단에서 import*한다(결정 4 — dep이라 import는 게이트 통과).
  - 그러나 **실 네트워크·실 OAuth·실 토큰은 생성·호출 시점에만 접촉**한다 — 모듈 import만으론
    클라이언트를 안 만들고 네트워크를 안 탄다. 게이트(`uv run pytest`)는 이 transport를 *주입하지
    않으므로*(워커/web 기본은 `StubProviderTransport`·`ClaudeCodeRuntime`) 결정론 테스트가
    실 SDK·네트워크를 절대 안 탄다. 게이트는 *import·타입*만 통과하면 된다(pyright·ruff·import).
  - 실 동작 검증은 수동 시연(T9.6·`scripts/demo_e2e_provider.md`)이다.

불변식:
  - **중앙 키/토큰 0** — `anthropic.Anthropic()`를 *인자 없이* 만든다. SDK가 owner의
    `ANTHROPIC_API_KEY` env 또는 `ant auth login`(공식 console OAuth) 프로필을 자동 해석한다.
    생성자에 api_key·auth_token을 *절대 주입하지 않는다*(중앙 토큰 0·결정 2·9).
  - **⚠️ 실 시연 정정(2026-06-27)** — 인자 없는 SDK는 **Claude Code `/login` 구독 자격**
    (`~/.claude/.credentials.json`·`claudeAiOauth`)은 *해석 못 한다*(다른 위치). 그 구독 토큰을
    직접 API에 쓰는 건 ToS/계정 정지 위험이라 *안 한다*. **Claude 구독 답은 `claude -p`
    (`ClaudeCodeRuntime`)가 공식·robust 경로**(실 시연 확인). 이 SDK transport는 *API 키/`ant`
    OAuth* owner용 인프로세스 빠른 경로다(codex는 자기 CLI 토큰 파일로 구독 인프로세스 — 비대칭).
  - **포트 무변경** — `__call__(request: ProviderRequest) -> Iterator[str]`(ProviderTransport
    Protocol 그대로). 청크를 yield하면 `assemble_stream`이 조립하고 `map_response_to_answer`가
    Answer로 매핑한다(노출 불변식·게이트 내 순수 함수 재사용).
"""

from collections.abc import Iterator
from typing import TYPE_CHECKING, cast

import anthropic

from agent_org_network.provider_runtime import ProviderRequest

if TYPE_CHECKING:
    from anthropic.types import MessageParam

# 스트리밍 기본 최대 토큰 — 긴 출력의 서버측 timeout 회피(claude-api 스킬 권장).
DEFAULT_MAX_TOKENS = 64_000


class AnthropicSdkTransport:
    """실 anthropic SDK 인프로세스 스트리밍 ProviderTransport (게이트 밖·T9.6).

    `client.messages.stream(...).text_stream`이 토큰 델타(`Iterator[str]`)를 내고 그대로
    yield한다 — 기존 `ProviderTransport.__call__(request) -> Iterable[str]`을 만족(포트·매핑
    함수 무변경·`StubProviderTransport`와 교체 가능).

    자격 위임(결정 2·9·실 시연 정정): 인자 없는 `anthropic.Anthropic()`가 owner의
    `ANTHROPIC_API_KEY` env 또는 `ant auth login`(공식 console OAuth) 프로필을 자동 해석한다 —
    중앙 토큰 주입 0. (Claude Code `/login` 구독 자격은 미해석·ToS/밴 위험 → 구독 답은 `claude -p`.)

    모델 기본값(결정 10): `claude-opus-4-8`(adaptive thinking·streaming). 단, 실제 모델은
    `request.model`이 권위다 — 어댑터(`ClaudeApiRuntime`)/구성이 `build_provider_request`로
    `ProviderRequest.model`을 정한다. 이 transport는 request.model이 비었을 때만 기본값을 쓴다.

    주의(opus-4-8/4.7 제약): `temperature`·`top_p`·`top_k`·`thinking.budget_tokens`를 보내면
    400이다 — 보내지 않는다. `thinking={"type": "adaptive"}`만 싣는다(budget 없음).
    """

    _DEFAULT_MODEL = "claude-opus-4-8"

    def __init__(self, *, max_tokens: int = DEFAULT_MAX_TOKENS) -> None:
        # 클라이언트는 *지연 생성*한다 — 모듈 import·인스턴스 생성만으론 실 자격 해석·네트워크를
        # 안 탄다(게이트가 이 transport를 주입해도 호출 전까진 무접촉). 첫 호출에서 인자 없는
        # Anthropic()를 만들어 owner ANTHROPIC_API_KEY/`ant` OAuth 프로필을 자동 해석한다(중앙 토큰 0).
        self._client: anthropic.Anthropic | None = None
        self._max_tokens = max_tokens

    def _ensure_client(self) -> anthropic.Anthropic:
        if self._client is None:
            # 인자 없이 — owner OAuth 프로필 자동 해석. 키/토큰 주입 금지(중앙 토큰 0·결정 2·9).
            self._client = anthropic.Anthropic()
        return self._client

    def __call__(self, request: ProviderRequest) -> Iterator[str]:
        client = self._ensure_client()
        model = request.model or self._DEFAULT_MODEL
        # ProviderRequest.messages는 공급자 중립 list[dict[str, str]]({role, content}). SDK는
        # Iterable[MessageParam](TypedDict·role 리터럴 제약)을 기대 — 런타임 형태는 동일하므로
        # cast로 경계를 넘긴다(역할 값은 build_provider_request가 "user"/"assistant"로 보증).
        messages = cast("list[MessageParam]", request.messages)
        with client.messages.stream(
            model=model,
            max_tokens=self._max_tokens,
            system=request.system,
            messages=messages,
            thinking={"type": "adaptive"},
        ) as stream:
            yield from stream.text_stream
