"""OKF author 선택 — `AON_AUTHOR` env 기반 시임(`runtime_select.select_runtime`과 대칭).

ADR 0029/0030 — owner측 OKF 자동 저작. 답 생성 경로(`runtime_select`)가 `AON_PROVIDER`로
런타임을 고르듯, 저작 경로는 `AON_AUTHOR`로 author 어댑터를 고른다.

**모든 분기가 실 추출이다**(실사용 경로 가짜 0 — T11.7d 사용자 결정·`runtime_select`와 같은
규약: env 시임은 *실* 어댑터 사이만 고른다. 결정론 테스트는 `create_app(author=FakeAuthor)`
주입으로 실 LLM·실 네트워크를 막는다 — env 폴백 더블 없음):

  - 미설정/`claude-api` → `LlmAuthor(AnthropicSdkTransport())` — owner OAuth 인프로세스
    anthropic SDK 실 추출(**프로덕션 기본**·중앙 토큰 0). anthropic은 선택 extra라 지연
    import·미설치면 명확한 SystemExit 안내(`runtime_select` 대칭).
  - `claude-code`/`llm` → `LlmAuthor(ClaudeCodeTransport())` — owner Claude *구독* 자격을
    `claude -p`로 위임하는 실 추출(ADR 0027 결정 9: 구독은 SDK가 자격을 못 읽어 CLI 위임).
  - 알 수 없는 값(구 `demo` 포함) → 명시 실패(SystemExit·조용한 폴백 없음·owner 의도 보존).

순환 import 회피: 모듈 레벨 import는 코어(`okf_authoring`)만이다. 실 transport는 각 분기에서만
*지연 import*한다 — 기본 경로도 호출 시점까지 SDK·subprocess를 안 건드린다.
"""

from __future__ import annotations

import os

from agent_org_network.okf_authoring import LlmAuthor, OkfAuthor

# author 기본 모델 — staged 추출(split/derive/link)용 균형 모델. `AON_AUTHOR_MODEL` env로
# 덮을 수 있다. 답변(`provider_transport_anthropic._DEFAULT_MODEL`=opus)·분류(haiku)와 같은
# *모듈 상수* 패턴 — 모델 교체는 이 한 줄만 바꾼다.
DEFAULT_AUTHOR_MODEL = "claude-sonnet-5"

# `AON_AUTHOR` 값(소문자 trim) → claude -p 위임 transport 사용 여부.
_CLAUDE_CODE_ALIASES = frozenset({"claude-code", "llm"})
# 미설정과 동치인 명시 별칭 — 프로덕션 기본(anthropic SDK 인프로세스).
_CLAUDE_API_ALIASES = frozenset({"claude-api", "anthropic"})


def _resolve_model() -> str:
    return (os.environ.get("AON_AUTHOR_MODEL") or "").strip() or DEFAULT_AUTHOR_MODEL


def select_author() -> OkfAuthor:
    """env 플래그로 OKF author를 고른다 — `runtime_select.select_runtime`과 대칭(ADR 0029/0030).

    카드 권한 domain 제약은 여기(생성 시점)가 아니라 파이프라인이 split 호출 시
    `card.domains`를 `allowed_domains` 인자로 넘겨 흐른다(`run_authoring_pipeline` —
    author는 카드 무관 재사용·선택은 어댑터만 책임).

    `AON_AUTHOR`(소문자 trim):
      - 미설정/`claude-api`/`anthropic` → `LlmAuthor(AnthropicSdkTransport())`(**기본**·실 추출).
      - `claude-code`/`llm` → `LlmAuthor(ClaudeCodeTransport())`(실 추출·`claude -p` 구독 위임).
      - 알 수 없는 값 → 명시 실패(SystemExit — 조용한 폴백·데모 더블 없음).

    모든 분기가 같은 `OkfAuthor` 포트라 호출 측(`/author/run` 파이프라인) 무변경.
    """
    flag = (os.environ.get("AON_AUTHOR") or "").strip().lower()
    model = _resolve_model()
    if not flag or flag in _CLAUDE_API_ALIASES:
        # 프로덕션 기본 — anthropic SDK는 선택 extra라 지연 import(미설치 owner 무접촉).
        try:
            from agent_org_network.provider_transport_anthropic import (
                AnthropicSdkTransport,
            )
        except ImportError as exc:
            raise SystemExit(
                "OKF 저작(/author/run)이 실 추출을 쓰는데 anthropic SDK가 없습니다 — "
                "공급자 extra를 설치하세요: pip install 'agent-org-network[claude-api]'  "
                "(uv: uv sync --extra claude-api). Claude *구독* 자격이면 "
                "AON_AUTHOR=claude-code(`claude -p` 위임)를 쓰세요."
            ) from exc
        return LlmAuthor(AnthropicSdkTransport(), model=model)
    if flag in _CLAUDE_CODE_ALIASES:
        from agent_org_network.provider_transport_claude_code import ClaudeCodeTransport

        print(
            f"[author_select] AON_AUTHOR={flag} → LlmAuthor(ClaudeCodeTransport, model={model}) "
            "— owner Claude 구독 자격 위임·실 추출."
        )
        return LlmAuthor(ClaudeCodeTransport(model=model), model=model)
    raise SystemExit(
        f"알 수 없는 AON_AUTHOR={flag!r} — 지원: claude-api(기본·anthropic SDK), "
        "claude-code/llm(`claude -p` 위임). 데모 더블은 제거됐다(실사용 가짜 0) — "
        "결정론 테스트는 create_app(author=FakeAuthor)로 주입하라."
    )
