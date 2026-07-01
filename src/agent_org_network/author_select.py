"""OKF author 선택 — `AON_AUTHOR` env 기반 시임(`runtime_select.select_runtime`과 대칭).

ADR 0029/0030 — owner측 OKF 자동 저작. 답 생성 경로(`runtime_select`)가 `AON_PROVIDER`로
런타임을 고르듯, 저작 경로는 `AON_AUTHOR`로 author 어댑터를 고른다.

  - 미설정/`demo` → `FakeAuthor`(데모 결정론 더블·**기본**). owner OAuth 없이도 `/author`가
    staged 개념·over-claim 드롭·커밋을 한 번 관통하게 한다(기존 게이트·테스트 무변경).
  - `claude-code`/`llm` → `LlmAuthor(ClaudeCodeTransport())` — owner Claude 구독 자격을
    `claude -p`로 위임하는 실 추출(게이트 밖). 입력 문서를 *실제로 본다*.
  - 알 수 없는 값 → 명시 실패(SystemExit·조용히 demo로 안 떨어진다·owner 의도 보존).

순환 import 회피: 모듈 레벨 import는 코어(`agent_card`·`okf_authoring`)만이다. 실 transport
(`ClaudeCodeTransport`)는 `claude-code` 분기에서만 *지연 import*한다 — demo 기본 경로는
transport·subprocess를 안 건드린다(`runtime_select`가 공급자 SDK를 지연 import하는 정신).
"""

from __future__ import annotations

import os

from agent_org_network.agent_card import AgentCard
from agent_org_network.okf_authoring import (
    FakeAuthor,
    OkfAuthor,
    OkfDocumentDraft,
)

# author 기본 모델 — split/derive/link 3회 순차 호출이라 지연을 줄이게 *빠른* 모델을 쓴다
# (opus는 느리다). `AON_AUTHOR_MODEL` env로 덮을 수 있다.
DEFAULT_AUTHOR_MODEL = "claude-sonnet-4-6"

# 어느 데모 카드도 권한으로 갖지 않는 라벨 — demo author의 over-claim 개념이 이 domain을 써서
# admit_okf가 dropped_concepts로 떨군다(저작면 over-claim 드롭 시연).
_DEMO_OVERCLAIM_DOMAIN = "기밀"


def build_demo_author(card: AgentCard) -> FakeAuthor:
    """데모용 결정론 author — 카드 owned domain 2건 + over-claim 1건을 고정 산출한다.

    owner OAuth 없이도 `/author` 데모가 staged 개념·over-claim 드롭·커밋을 한 번 관통하게
    한다(ADR 0030 결정 4 "얇은 수직 슬라이스"). 입력 문서와 무관하게 고정 산출한다(데모
    결정론) — 실 추출은 `LlmAuthor`가 입력을 본다(`AON_AUTHOR=claude-code`).

    카드 domains에서 in-domain 개념 2건을 만들고(없으면 1건), over-claim 개념 1건은
    `_DEMO_OVERCLAIM_DOMAIN`(어느 데모 카드도 권한 없는 라벨)으로 만들어 admit_okf가 떨구게 한다.
    """
    domains = list(card.domains)
    in_domain = domains[:2] if len(domains) >= 2 else (domains or [_DEMO_OVERCLAIM_DOMAIN])
    docs: list[OkfDocumentDraft] = []
    for i, dom in enumerate(in_domain):
        docs.append(
            OkfDocumentDraft(
                concept_id=f"demo-{card.agent_id}-{i + 1}",
                title=f"{dom} 정책 요약",
                body=f"{dom}에 대한 기준과 처리 절차를 정리한 개념입니다(데모 자동 초안).",
                core_question=f"{dom}은 어떻게 처리하나요?",
                domain=dom,
            )
        )
    # over-claim 개념 1건 — 카드 권한 밖 domain이라 admit_okf가 dropped_concepts로 떨군다.
    docs.append(
        OkfDocumentDraft(
            concept_id=f"demo-{card.agent_id}-nda",
            title="기밀유지(NDA) 규정",
            body="권한 밖 도메인의 개념입니다 — admit_okf가 over-claim으로 떨굽니다(데모).",
            core_question="NDA는 어떻게 처리하나요?",
            domain=_DEMO_OVERCLAIM_DOMAIN,
        )
    )
    fixed = tuple(docs)
    return FakeAuthor(split_result=fixed, derive_result=fixed, link_result=())


# `AON_AUTHOR` 값(소문자 trim) → 실 LlmAuthor 사용 여부. demo/미설정은 기본 분기에서 처리.
_LLM_AUTHOR_ALIASES = frozenset({"claude-code", "llm"})


def select_author(card: AgentCard) -> OkfAuthor:
    """env 플래그로 OKF author를 고른다 — `runtime_select.select_runtime`과 대칭(ADR 0029/0030).

    `AON_AUTHOR`(소문자 trim):
      - 미설정/`demo` → `build_demo_author(card)`(데모 더블·**기본**·게이트·기존 테스트 무변경).
      - `claude-code`/`llm` → `LlmAuthor(ClaudeCodeTransport(), model=...)`(실 추출·게이트 밖).
        모델은 `AON_AUTHOR_MODEL` env 있으면 그 값·없으면 `DEFAULT_AUTHOR_MODEL`(빠른 sonnet).
      - 알 수 없는 값 → 명시 실패(SystemExit — 조용히 demo로 안 떨어진다·owner 의도 보존).

    모든 분기가 같은 `OkfAuthor` 포트라 호출 측(`/author/run` 파이프라인) 무변경. 실 transport
    import는 `claude-code` 분기에서만 — demo 기본 경로는 transport·subprocess를 안 건드린다.
    """
    flag = (os.environ.get("AON_AUTHOR") or "").strip().lower()
    if not flag or flag == "demo":
        return build_demo_author(card)
    if flag in _LLM_AUTHOR_ALIASES:
        # 실 transport는 이 분기에서만 지연 import(demo 기본은 무접촉).
        from agent_org_network.okf_authoring import LlmAuthor
        from agent_org_network.provider_transport_claude_code import ClaudeCodeTransport

        model = (os.environ.get("AON_AUTHOR_MODEL") or "").strip() or DEFAULT_AUTHOR_MODEL
        print(
            f"[author_select] AON_AUTHOR={flag} → LlmAuthor(ClaudeCodeTransport, model={model}) "
            "— owner Claude 구독 자격 위임·실 추출(게이트 밖)."
        )
        # 유효 domain 제약은 파이프라인이 split 호출 시 card.domains를 allowed_domains로 넘긴다
        # (`run_authoring_pipeline` — LLM이 in-scope 개념에 정확한 domain을 골라 admit_okf가
        # over-claim으로 전량 드롭하지 않는다·치명 갭 교정).
        return LlmAuthor(ClaudeCodeTransport(model=model), model=model)
    raise SystemExit(
        f"알 수 없는 AON_AUTHOR={flag!r} — 지원: demo(기본), claude-code/llm"
    )
