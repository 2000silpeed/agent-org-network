"""S2 — ConfluenceIngestor 매핑 red→green (Phase 14·ADR 0039).

Confluence 페이지 payload(dict) → RawSource(source_id 호출자 제공·본문 관대 추출).
회사 자체 개발 MCP는 기능 제약이 많고 페이지 읽기만 가능(2026-07-08 확정) — 특정
벤더 payload 모양에 하드코딩하지 않고, 본문 텍스트를 관대한 폴백 사슬로 뽑는다.
실 페치(S4)는 이 매핑에 라이브 호출만 얹는 얇은 껍데기.

Ingestor 포트 계약: ingest(items) -> tuple[RawSource, ...], N입력→N RawSource(1:1·
분할 안 함), source_id는 호출자 제공(어댑터가 생성/해시 안 함). TextIngestor(같은
파일 okf_authoring.py)의 (source_id, str) 동형을 (source_id, payload dict)로 확장.
"""

from __future__ import annotations

from collections.abc import Mapping

import pydantic
import pytest

from agent_org_network.confluence_ingestor import ConfluenceIngestor
from agent_org_network.okf_authoring import Ingestor, RawSource


def test_rest_storage_shape() -> None:
    """raw REST v1 shape(body.storage.value) → 태그 제거된 텍스트·source_id 보존."""
    ingestor = ConfluenceIngestor()
    result = ingestor.ingest(
        [("page-1", {"body": {"storage": {"value": "<p>환불은 7일 이내 가능합니다.</p>"}}})]
    )
    assert len(result) == 1
    assert result[0].source_id == "page-1"
    assert result[0].content == "환불은 7일 이내 가능합니다."


def test_plain_body_string_shape() -> None:
    """제약 많은 자체 MCP가 body를 이미 정규화된 str로 반환 → 그대로 content."""
    ingestor = ConfluenceIngestor()
    result = ingestor.ingest([("page-2", {"body": "환불은 7일 이내 가능합니다."})])
    assert len(result) == 1
    assert result[0].content == "환불은 7일 이내 가능합니다."


def test_toplevel_content_or_text_fallback() -> None:
    """body 자체가 없고 top-level content/text만 있을 때 폴백 추출."""
    ingestor = ConfluenceIngestor()

    result_content = ingestor.ingest([("page-3", {"content": "환불은 7일 이내."})])
    assert result_content[0].content == "환불은 7일 이내."

    result_text = ingestor.ingest([("page-4", {"text": "환불은 7일 이내."})])
    assert result_text[0].content == "환불은 7일 이내."


def test_missing_body_yields_empty_content() -> None:
    """본문 경로(body.storage.value·body str·content·text) 전부 부재 → 추출 결과는
    빈 문자열이며, RawSource.content 불변식(T11.1 — 빈/공백 거부)에 그대로 위임한다.

    TextIngestor의 빈 content 처리(같은 파일 okf_authoring.py)와 동일 패턴 —
    ConfluenceIngestor가 스킵으로 조용히 항목을 누락시키지 않는다(스킵 아님).
    N:N 계약은 "유효 항목 성공 시 정확히 N개" 형태로 지켜지고, 도메인상 무의미한
    빈 콘텐츠는 (스킵이 아니라) 명시적 실패로 경계를 드러낸다.
    """
    ingestor = ConfluenceIngestor()
    with pytest.raises(pydantic.ValidationError):
        ingestor.ingest([("page-5", {"id": "12345", "type": "page"})])


def test_html_entities_and_whitespace_normalized() -> None:
    """HTML 엔티티 디코드·다중 공백/개행 정리."""
    ingestor = ConfluenceIngestor()
    result = ingestor.ingest(
        [
            (
                "page-6",
                {
                    "body": {
                        "storage": {
                            "value": "<p>이용약관은  &amp;\n\n유의사항을   포함합니다.</p>"
                        }
                    }
                },
            )
        ]
    )
    assert result[0].content == "이용약관은 & 유의사항을 포함합니다."


def test_source_id_is_caller_provided_not_derived() -> None:
    """같은 payload에 payload 내부 id가 있어도 RawSource.source_id는 호출자 값 그대로."""
    ingestor = ConfluenceIngestor()
    payload = {"id": "12345", "body": {"storage": {"value": "<p>본문</p>"}}}

    result_a = ingestor.ingest([("caller-id-a", payload)])
    result_b = ingestor.ingest([("caller-id-b", payload)])

    assert result_a[0].source_id == "caller-id-a"
    assert result_b[0].source_id == "caller-id-b"
    assert result_a[0].content == result_b[0].content == "본문"


def test_one_to_one_order_preserved() -> None:
    """N item → N RawSource·순서 보존(분할 안 함)."""
    ingestor = ConfluenceIngestor()
    items = [
        ("page-a", {"body": {"storage": {"value": "<p>내용a</p>"}}}),
        ("page-b", {"body": "내용b"}),
        ("page-c", {"content": "내용c"}),
    ]
    result = ingestor.ingest(items)
    assert len(result) == 3
    assert [r.source_id for r in result] == ["page-a", "page-b", "page-c"]
    assert [r.content for r in result] == ["내용a", "내용b", "내용c"]


def test_pure_deterministic() -> None:
    """동일 입력 2회 → 동일 출력(순수·결정론·IO 0)."""
    ingestor = ConfluenceIngestor()
    items = [("page-1", {"body": {"storage": {"value": "<p>환불 정책</p>"}}})]
    result1 = ingestor.ingest(items)
    result2 = ingestor.ingest(items)
    assert result1 == result2


def test_nested_html_tags_stripped() -> None:
    """중첩 태그·리스트·표 → 텍스트만 남고 내용 누락 0(거친 strip 허용)."""
    ingestor = ConfluenceIngestor()
    html_body = (
        "<div><ul><li>사업자 등록번호: 123-45-67890</li>"
        "<li>대표: 홍길동</li></ul>"
        "<table><tr><td>날짜</td><td>내용</td></tr></table></div>"
    )
    result = ingestor.ingest([("page-7", {"body": {"storage": {"value": html_body}}})])
    content = result[0].content
    assert "<" not in content
    assert ">" not in content
    assert "사업자 등록번호: 123-45-67890" in content
    assert "대표: 홍길동" in content
    assert "날짜" in content
    assert "내용" in content


def test_raw_source_type_unchanged() -> None:
    """산출은 기존 RawSource 그대로(새 값객체 발명 없음)."""
    ingestor = ConfluenceIngestor()
    result = ingestor.ingest([("page-1", {"body": "본문"})])
    assert isinstance(result[0], RawSource)


def test_포트_준수_Ingestor_Mapping_Protocol() -> None:
    """ConfluenceIngestor가 Ingestor[Mapping[str, object]] Protocol을 만족한다
    (제네릭화 — okf_authoring.Ingestor, ADR 0039 결정 3)."""

    def _accept(i: Ingestor[Mapping[str, object]]) -> None:
        pass

    _accept(ConfluenceIngestor())
