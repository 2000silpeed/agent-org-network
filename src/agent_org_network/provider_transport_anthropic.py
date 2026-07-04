"""T9.6 — 실 anthropic SDK ProviderTransport (ADR 0027 결정 2·4·9·10·ADR 0033 결정 2) [게이트 밖]

`StubProviderTransport`(게이트 기본·결정론)와 *같은 Protocol*(`ProviderTransport`)을 만족하는
**실 공급자 transport**. 공식 anthropic SDK 인프로세스 스트리밍.

게이트 경계(정직한 분리):
  - 이 모듈은 `anthropic` SDK를 *모듈 상단에서 import*한다(결정 4 — dep이라 import는 게이트 통과).
  - 그러나 **실 네트워크·실 키는 생성·호출 시점에만 접촉**한다 — 모듈 import만으론
    클라이언트를 안 만들고 네트워크를 안 탄다. 게이트(`uv run pytest`)는 이 transport를 *주입하지
    않으므로*(워커/web 기본은 `StubProviderTransport`·`ClaudeCodeRuntime`) 결정론 테스트가
    실 SDK·네트워크를 절대 안 탄다. 게이트는 *import·타입*만 통과하면 된다(pyright·ruff·import).
  - 실 동작 검증은 수동 시연(T9.6·`scripts/demo_e2e_provider.md`)이다.

자격증명 모델 전환(ADR 0033 결정 2 — "중앙 토큰 0" 정직 폐기):
  - **Phase 12 이전(ADR 0027)**: 인자 없는 `anthropic.Anthropic()`로 owner OAuth 프로필 자동
    해석(중앙 토큰 0). 답 실행이 owner 워커였으므로 자격증명도 owner측이었다.
  - **Phase 12(ADR 0033 결정 1·2)**: 답 실행이 *중앙 런타임*으로 이동하면서 자격증명이 중앙으로
    갈 수밖에 없다 — "중앙 토큰 0"을 정직하게 폐기하고 **중앙 조직 API 키 1개**로 부른다.
    중앙 서버(`create_central_app`)가 이 transport를 쓸 때 키를 `AON_PROVIDER_KEY`(우선) 또는
    표준 `ANTHROPIC_API_KEY` env에서만 로딩한다(코드·저장소·와이어에 싣지 않음).

대체 안전장치(ADR 0033 결정 2 — 폐기의 반대급부로 반드시 둔다):
  - **키 보관** — 키는 env/시크릿에서만 로딩한다(생성자에 원문 하드코딩 금지). env 미설정이면
    인자 없는 SDK 폴백(owner ANTHROPIC_API_KEY/`ant` OAuth 자동 해석 — 하위호환·게이트 밖 owner 경로).
  - **로그에 키 미노출** — 이 transport는 키 원문을 print/log에 절대 싣지 않는다(비용 태깅은
    `agent_id` 식별자로만). audit·트랜스크립트에도 키가 안 흐른다(호출측 Answer 계약이 식별자만).
  - **비용 귀속 = 태깅** — `ProviderRequest`에 `agent_id`가 있으면(호출측 태깅) SDK `metadata`의
    `user_id`로 실어 사후 집계가 가능하게 한다(담당자별 비용 구분·키를 담당자별로 쪼개지 않음).

불변식:
  - **⚠️ 실 시연 정정(2026-06-27)** — Claude Code `/login` 구독 자격은 SDK가 미해석·ToS/밴
    위험이라 직접 API에 안 쓴다. Claude 구독 답은 `claude -p`(`ClaudeCodeRuntime`)가 공식 경로.
    이 SDK transport는 *API 키* 경로다(중앙 조직 키·ADR 0033 결정 2).
  - **포트 무변경** — `__call__(request: ProviderRequest) -> Iterator[str]`(ProviderTransport
    Protocol 그대로). 청크를 yield하면 `assemble_stream`이 조립하고 `map_response_to_answer`가
    Answer로 매핑한다(노출 불변식·게이트 내 순수 함수 재사용).
"""

import os
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, cast

import anthropic

from agent_org_network.provider_runtime import ProviderRequest

if TYPE_CHECKING:
    from anthropic.types import MessageParam

# 스트리밍 기본 최대 토큰 — 긴 출력의 서버측 timeout 회피(claude-api 스킬 권장).
DEFAULT_MAX_TOKENS = 64_000


def load_central_org_key() -> str | None:
    """중앙 조직 API 키를 env에서만 로딩한다(ADR 0033 결정 2 — 키 보관 안전장치).

    `AON_PROVIDER_KEY`(우선·이 프로젝트 명시 시임) → `ANTHROPIC_API_KEY`(표준) 순으로 본다.
    둘 다 미설정이면 None(인자 없는 SDK 폴백 — owner 자동 해석·하위호환). 키 원문은
    *반환값으로만* 흐르고 로그/print에 절대 싣지 않는다(로그 미노출 안전장치).
    """
    for var in ("AON_PROVIDER_KEY", "ANTHROPIC_API_KEY"):
        raw = (os.environ.get(var) or "").strip()
        if raw:
            return raw
    return None


class AnthropicSdkTransport:
    """실 anthropic SDK 인프로세스 스트리밍 ProviderTransport (게이트 밖·T9.6·ADR 0033 결정 2).

    `client.messages.stream(...).text_stream`이 토큰 델타(`Iterator[str]`)를 내고 그대로
    yield한다 — 기존 `ProviderTransport.__call__(request) -> Iterable[str]`을 만족(포트·매핑
    함수 무변경·`StubProviderTransport`와 교체 가능).

    자격증명(ADR 0033 결정 2·중앙 조직 키): 생성 시 `AON_PROVIDER_KEY`/`ANTHROPIC_API_KEY`
    env에서 중앙 조직 키를 로딩해 클라이언트에 주입한다. env 미설정이면 인자 없는 SDK 폴백
    (owner 자동 해석·하위호환·게이트 밖 owner 경로). 키 원문은 로그에 안 싣는다.

    모델 기본값(결정 10): `claude-opus-4-8`(adaptive thinking·streaming). 단, 실제 모델은
    `request.model`이 권위다 — 어댑터(`ClaudeApiRuntime`)/구성이 `build_provider_request`로
    `ProviderRequest.model`을 정한다. 이 transport는 request.model이 비었을 때만 기본값을 쓴다.

    주의(opus-4-8/4.7 제약): `temperature`·`top_p`·`top_k`·`thinking.budget_tokens`를 보내면
    400이다 — 보내지 않는다. `thinking={"type": "adaptive"}`만 싣는다(budget 없음).
    """

    _DEFAULT_MODEL = "claude-opus-4-8"

    def __init__(
        self, *, max_tokens: int = DEFAULT_MAX_TOKENS, api_key: str | None = None
    ) -> None:
        # 클라이언트는 *지연 생성*한다 — 모듈 import·인스턴스 생성만으론 실 자격 해석·네트워크를
        # 안 탄다(게이트가 이 transport를 주입해도 호출 전까진 무접촉). 첫 호출에서 클라이언트를
        # 만든다. api_key는 명시 주입 seam(테스트/구성) — 미주입이면 `_ensure_client`가
        # env(`AON_PROVIDER_KEY`/`ANTHROPIC_API_KEY`)에서 중앙 조직 키를 로딩한다(ADR 0033 결정 2).
        self._client: anthropic.Anthropic | None = None
        self._max_tokens = max_tokens
        self._api_key = api_key

    def _ensure_client(self) -> anthropic.Anthropic:
        if self._client is None:
            key = self._api_key if self._api_key is not None else load_central_org_key()
            if key:
                # 중앙 조직 키 주입(ADR 0033 결정 2 — "중앙 토큰 0" 정직 폐기). 키 원문은
                # 여기서만 SDK에 넘기고 어디에도 로깅하지 않는다(로그 미노출 안전장치).
                self._client = anthropic.Anthropic(api_key=key)
            else:
                # env 미설정 폴백 — 인자 없이 owner ANTHROPIC_API_KEY/`ant` OAuth 자동 해석
                # (게이트 밖 owner 경로·하위호환).
                self._client = anthropic.Anthropic()
        return self._client

    def __call__(self, request: ProviderRequest) -> Iterator[str]:
        client = self._ensure_client()
        model = request.model or self._DEFAULT_MODEL
        # ProviderRequest.messages는 공급자 중립 list[dict[str, str]]({role, content}). SDK는
        # Iterable[MessageParam](TypedDict·role 리터럴 제약)을 기대 — 런타임 형태는 동일하므로
        # cast로 경계를 넘긴다(역할 값은 build_provider_request가 "user"/"assistant"로 보증).
        messages = cast("list[MessageParam]", request.messages)
        # 비용 태깅(ADR 0033 결정 2): agent_id가 있으면 SDK metadata.user_id로 실어 중앙
        # 조직 키 1개 하에서도 담당자별 비용을 사후 집계할 수 있게 한다(키를 담당자별로
        # 쪼개지 않음). 식별자만 흐르고 키는 안 흐른다(노출 불변식). 없으면 태깅 없음.
        stream_kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": self._max_tokens,
            "system": request.system,
            "messages": messages,
            "thinking": {"type": "adaptive"},
        }
        if request.agent_id:
            stream_kwargs["metadata"] = {"user_id": request.agent_id}
        with client.messages.stream(**stream_kwargs) as stream:
            yield from stream.text_stream
