"""S2 — ConfluenceIngestor: Confluence 페이지 payload → RawSource 매핑(ADR 0039).

`Ingestor` 포트(okf_authoring.py)의 실 어댑터 하나 — `TextIngestor`의 (source_id, str)
동형을 (source_id, payload dict)로 확장한다. 새 포트·새 값객체는 만들지 않는다
(`Ingestor`·`RawSource` 재사용).

회사 자체 개발 MCP는 기능 제약이 많고 스페이스 페이지 읽기만 가능(2026-07-08 확정) —
접근 방식이 공식 Atlassian Remote MCP(Rovo)·커뮤니티 MCP·raw REST 중 무엇이든 wire
shape가 다를 수 있어, 특정 벤더 payload 모양에 하드코딩하지 않는다. 본문 텍스트를
관대한 폴백 사슬로 뽑아 어떤 shape든 픽스처 교체만으로 흡수한다.

본문 추출 폴백 사슬:
  1. body.storage.value(raw REST HTML)
  2. body가 str(이미 정규화된 자체 MCP 응답)
  3. top-level content 또는 text
  4. 없으면 빈 문자열

추출 텍스트는 거친 HTML 태그 제거 + 엔티티 디코드 + 공백 정리를 거친다(stdlib만·
정밀 마크다운 변환은 후속). 오너십 메타(관리자·라벨·작성자) 추출은 S2 범위 밖
(S1 `ConfluenceOwnershipSignal`·S4 페처 몫) — 이 모듈은 순수 콘텐츠→RawSource.

본문 완전 부재(추출 결과 빈 문자열)는 `TextIngestor`와 동일하게 `RawSource.content`
불변식(T11.1 — 빈/공백 거부)에 위임한다. 스킵으로 조용히 항목을 누락시키지 않고
명시적 실패로 경계를 드러낸다 — 페이지 단위 격리(한 페이지 이슈로 배치 전체가
실패하지 않게 하는 것)는 S2 범위 밖(S4 페처가 페이지별로 흡수할 몫).
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping, Sequence
from typing import cast

from agent_org_network.okf_authoring import RawSource

_TAG_RE: re.Pattern[str] = re.compile(r"<[^>]+>")
_WHITESPACE_RE: re.Pattern[str] = re.compile(r"\s+")


def _normalize_extracted_text(raw: str) -> str:
    """거친 HTML 정규화 — 태그를 공백으로 치환(단어 경계 보존)·엔티티 디코드·공백 정리."""
    without_tags = _TAG_RE.sub(" ", raw)
    decoded = html.unescape(without_tags)
    collapsed = _WHITESPACE_RE.sub(" ", decoded)
    return collapsed.strip()


def _extract_body_text(payload: Mapping[str, object]) -> str:
    """Confluence 페이지 payload에서 본문 텍스트를 관대한 폴백 사슬로 추출."""
    body: object = payload.get("body")
    if isinstance(body, Mapping):
        body_mapping = cast("Mapping[str, object]", body)
        storage: object = body_mapping.get("storage")
        if isinstance(storage, Mapping):
            storage_mapping = cast("Mapping[str, object]", storage)
            value: object = storage_mapping.get("value")
            if isinstance(value, str):
                return _normalize_extracted_text(value)
    elif isinstance(body, str):
        return _normalize_extracted_text(body)

    content: object = payload.get("content")
    if isinstance(content, str):
        return _normalize_extracted_text(content)

    text: object = payload.get("text")
    if isinstance(text, str):
        return _normalize_extracted_text(text)

    return ""


class ConfluenceIngestor:
    """`Ingestor[Mapping[str, object]]` 구현 — `Ingestor` 포트의 실 어댑터.

    각 원소 = (source_id, payload). source_id는 호출자 제공(어댑터가 생성/해시
    안 함 — 증분 매칭 키 안정성·ADR 0029 T11.6과 동일 계약). payload는 Confluence
    페이지 dict — 정확한 wire shape는 관대한 폴백 사슬(`_extract_body_text`)로 흡수.

    `okf_authoring.Ingestor`는 제네릭 Protocol(`Ingestor[T]`)이라 `TextIngestor`
    (`Ingestor[str]`)와 이 어댑터(`Ingestor[Mapping[str, object]]`)가 같은 포트를
    서로 다른 payload 타입으로 만족한다(ADR 0039 결정 3 — Confluence = Ingestor
    포트 어댑터).
    """

    def ingest(
        self, items: Sequence[tuple[str, Mapping[str, object]]]
    ) -> tuple[RawSource, ...]:
        """(source_id, payload) 시퀀스 → RawSource 튜플(순서 보존·분할 안 함)."""
        return tuple(
            RawSource(source_id=source_id.strip(), content=_extract_body_text(payload))
            for source_id, payload in items
        )
