"""Embedder 선택 — `AON_EMBEDDER` env 기반 시임(`select_author`·`select_runtime` 대칭).

ADR 0032 결정 C·OQ-4 — near-dup 후보 탐지용 임베더를 owner가 env로 고른다. 답 생성
경로(`select_runtime`)가 `AON_PROVIDER`로, 저작 경로(`select_author`)가 `AON_AUTHOR`로
고르듯, dedup 경로는 `AON_EMBEDDER`로 임베더를 고른다.

  - 미설정/`demo`/`off` → `None`(**운영 기본 = dedup 비활성**). dedup 라우트는 임베더가
    `None`이면 임베딩·cosine을 *건너뛰고 빈 후보*를 낸다(ADR 0032 §C 299행: "기본 경로는
    임베딩 의존성 없이도 빈 후보로 통과·extra 미설치 owner 무영향"). `FakeEmbedder`는
    생성자가 고정 dict를 요구하는 *결정론 테스트 더블*이라 운영 기본값으로 못 쓴다 — 운영
    비활성은 `None`으로 표현하고, 결정론 테스트는 `select_embedder`를 모킹해 `FakeEmbedder`를
    주입한다(라우트 테스트 패턴).
  - `fastembed` → 실 `FastEmbedEmbedder`(owner측 로컬 ONNX·게이트 밖·dedup extra 필요).
  - 알 수 없는 값 → 명시 실패(SystemExit·조용히 비활성으로 안 떨어진다·owner 의도 보존).

순환 import 회피: 모듈 레벨 import는 없다(반환 타입 `Embedder`는 TYPE_CHECKING). 실
어댑터(`FastEmbedEmbedder`)는 `fastembed` 분기에서만 *지연 import*한다 — demo/비활성
기본 경로는 fastembed·ONNX를 안 건드린다(`select_author`가 transport를 지연 import하는 정신).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_org_network.okf_dedup import Embedder

# `AON_EMBEDDER` 값(소문자 trim) → 실 FastEmbedEmbedder 사용 여부. demo/off/미설정은 None.
_FASTEMBED_ALIASES = frozenset({"fastembed"})
_DISABLED_ALIASES = frozenset({"", "demo", "off"})


def select_embedder() -> "Embedder | None":
    """env 플래그로 near-dup 임베더를 고른다 — `select_author`·`select_runtime`과 대칭.

    `AON_EMBEDDER`(소문자 trim):
      - 미설정/`demo`/`off` → `None`(**운영 기본 = 비활성**). 호출 측(dedup 라우트)이 `None`을
        보면 임베딩을 건너뛰고 빈 후보를 낸다(extra 미설치 owner 무영향·미아 없음 보존).
      - `fastembed` → `FastEmbedEmbedder()`(실 ONNX·게이트 밖·dedup extra 필요).
      - 알 수 없는 값 → 명시 실패(SystemExit — 조용히 비활성으로 안 떨어진다).

    실 어댑터 import는 `fastembed` 분기에서만 — demo/비활성 기본 경로는 fastembed를 안 건드린다.
    """
    flag = (os.environ.get("AON_EMBEDDER") or "").strip().lower()
    if flag in _DISABLED_ALIASES:
        return None
    if flag in _FASTEMBED_ALIASES:
        # 실 어댑터는 이 분기에서만 지연 import(demo/비활성 기본은 무접촉).
        from agent_org_network.provider_embed_fastembed import FastEmbedEmbedder

        print(
            f"[embedder_select] AON_EMBEDDER={flag} → FastEmbedEmbedder(intfloat/"
            "multilingual-e5-small) — owner측 로컬 ONNX 임베딩·중앙 토큰 0(게이트 밖)."
        )
        return FastEmbedEmbedder()
    raise SystemExit(
        f"알 수 없는 AON_EMBEDDER={flag!r} — 지원: demo/off(기본·비활성), fastembed"
    )
