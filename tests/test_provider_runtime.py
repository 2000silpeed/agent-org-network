"""T9.4(a)(b) — 공급자 런타임 어댑터 shape + 순수 함수 [게이트 내·결정론]

슬라이스 (a): ClaudeApiRuntime(AgentRuntime 포트) + ProviderTransport(주입 seam) + StubProviderTransport
슬라이스 (b): build_provider_request · assemble_stream · map_response_to_answer (순수 함수, SDK/IO 0)

불변식:
- Answer 계약 보존: text·sources·mode·snapshot_sha 필드만·새 필드 없음
- 노출 불변식: 매핑이 내부값·비밀 누출 0
- Authority 중앙: 런타임은 답 생성이지 권한 선언 아님
- 주입 transport 결정론: StubProviderTransport로 실 네트워크/SDK 0
- NotImplementedError 자리: CodexApiRuntime·GeminiApiRuntime 후속 공급자
"""

from collections.abc import Iterable
from datetime import date

import pytest

from agent_org_network.agent_card import AgentCard
from agent_org_network.provider_runtime import (
    ClaudeApiRuntime,
    CodexApiRuntime,
    GeminiApiRuntime,
    ProviderRequest,
    ProviderTransport,
    StubProviderTransport,
    assemble_stream,
    build_provider_request,
    map_response_to_answer,
)
from agent_org_network.provider_retry import ProviderAuthError, RetryPolicy
from agent_org_network.runtime import Answer, AnswerChunk, StreamingRuntime


# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------


@pytest.fixture()
def card() -> AgentCard:
    return AgentCard(
        agent_id="cs_ops",
        owner="alice",
        team="CS팀",
        summary="고객 서비스 운영 담당",
        domains=["고객지원", "환불"],
        last_reviewed_at=date(2026, 6, 27),
        knowledge_sources=["cs_ops/policy.md", "cs_ops/faq.md"],
    )


@pytest.fixture()
def minimal_card() -> AgentCard:
    return AgentCard(
        agent_id="devops-lead",
        owner="bob",
        team="인프라팀",
        summary="DevOps 담당",
        domains=["배포"],
        last_reviewed_at=date(2026, 6, 27),
    )


# ---------------------------------------------------------------------------
# 슬라이스 (a) — ProviderTransport Protocol
# ---------------------------------------------------------------------------


class TestProviderTransportProtocol:
    def test_stub_transport는_Iterable_str을_반환한다(self) -> None:
        transport = StubProviderTransport(chunks=["안녕", "하세요"])
        req = ProviderRequest(model="claude-3-5-haiku-20241022", messages=[{"role": "user", "content": "테스트"}])
        result = transport(req)
        assert list(result) == ["안녕", "하세요"]

    def test_stub_transport는_기본_청크_시퀀스를_가진다(self) -> None:
        transport = StubProviderTransport()
        req = ProviderRequest(model="claude-3-5-haiku-20241022", messages=[])
        chunks = list(transport(req))
        assert len(chunks) >= 1
        assembled = "".join(chunks)
        assert len(assembled) > 0

    def test_stub_transport는_결정론적이다(self, card: AgentCard) -> None:
        chunks_fixed = ["고정", " 응답", " 텍스트"]
        transport = StubProviderTransport(chunks=chunks_fixed)
        req = ProviderRequest(model="claude-3-5-haiku-20241022", messages=[{"role": "user", "content": "질문"}])
        result1 = list(transport(req))
        result2 = list(transport(req))
        assert result1 == result2 == chunks_fixed

    def test_stub_transport는_빈_청크_허용(self) -> None:
        transport = StubProviderTransport(chunks=[])
        req = ProviderRequest(model="claude-3-5-haiku-20241022", messages=[])
        assert list(transport(req)) == []

    def test_ProviderTransport는_Protocol_구조적_타이핑_만족(self) -> None:
        transport: ProviderTransport = StubProviderTransport(chunks=["x"])
        req = ProviderRequest(model="m", messages=[])
        result: Iterable[str] = transport(req)
        assert list(result) == ["x"]


# ---------------------------------------------------------------------------
# 슬라이스 (a) — ProviderRequest 값 객체
# ---------------------------------------------------------------------------


class TestProviderRequest:
    def test_provider_request는_frozen이다(self) -> None:
        from pydantic import ValidationError

        req = ProviderRequest(model="claude-3-5-haiku-20241022", messages=[])
        with pytest.raises((AttributeError, TypeError, ValidationError)):
            req.model = "other"  # type: ignore[misc]

    def test_provider_request_최소_필드(self) -> None:
        req = ProviderRequest(model="claude-3-5-haiku-20241022", messages=[])
        assert req.model == "claude-3-5-haiku-20241022"
        assert req.messages == []

    def test_provider_request_메시지_포함(self) -> None:
        msgs = [{"role": "user", "content": "안녕"}]
        req = ProviderRequest(model="m", messages=msgs)
        assert req.messages == msgs


# ---------------------------------------------------------------------------
# 슬라이스 (a) — ClaudeApiRuntime (AgentRuntime 포트 구현)
# ---------------------------------------------------------------------------


class TestClaudeApiRuntime:
    def test_stub_transport_주입으로_answer가_Answer를_반환한다(self, card: AgentCard) -> None:
        transport = StubProviderTransport(chunks=["테스트 답변입니다."])
        runtime = ClaudeApiRuntime(transport=transport)
        answer = runtime.answer("환불 정책이 어떻게 되나요?", card)
        assert isinstance(answer, Answer)

    def test_answer_text에_transport_청크_조립이_반영된다(self, card: AgentCard) -> None:
        transport = StubProviderTransport(chunks=["환불은 ", "30일 ", "이내입니다."])
        runtime = ClaudeApiRuntime(transport=transport)
        answer = runtime.answer("환불 정책?", card)
        assert answer.text == "환불은 30일 이내입니다."

    def test_answer_sources에_카드_knowledge_sources가_실린다(self, card: AgentCard) -> None:
        transport = StubProviderTransport(chunks=["답"])
        runtime = ClaudeApiRuntime(transport=transport)
        answer = runtime.answer("질문", card)
        assert answer.sources == tuple(card.knowledge_sources)

    def test_answer_mode는_full이다(self, card: AgentCard) -> None:
        transport = StubProviderTransport(chunks=["답"])
        runtime = ClaudeApiRuntime(transport=transport)
        answer = runtime.answer("질문", card)
        assert answer.mode == "full"

    def test_answer_snapshot_sha는_None이다(self, card: AgentCard) -> None:
        transport = StubProviderTransport(chunks=["답"])
        runtime = ClaudeApiRuntime(transport=transport)
        answer = runtime.answer("질문", card)
        assert answer.snapshot_sha is None

    def test_answer는_새_필드를_만들지_않는다(self, card: AgentCard) -> None:
        transport = StubProviderTransport(chunks=["답"])
        runtime = ClaudeApiRuntime(transport=transport)
        answer = runtime.answer("질문", card)
        assert set(answer.__dataclass_fields__) == {"text", "sources", "mode", "snapshot_sha"}

    def test_knowledge_sources_없는_카드는_빈_sources(self, minimal_card: AgentCard) -> None:
        transport = StubProviderTransport(chunks=["답변"])
        runtime = ClaudeApiRuntime(transport=transport)
        answer = runtime.answer("배포 방법?", minimal_card)
        assert answer.sources == ()

    def test_빈_청크_조립은_빈_text로_남지_않고_기본값_사용(self, card: AgentCard) -> None:
        transport = StubProviderTransport(chunks=[""])
        runtime = ClaudeApiRuntime(transport=transport)
        answer = runtime.answer("질문", card)
        assert isinstance(answer, Answer)

    def test_transport는_실_네트워크_SDK_없이_동작한다(self, card: AgentCard) -> None:
        transport = StubProviderTransport(chunks=["결정론 응답"])
        runtime = ClaudeApiRuntime(transport=transport)
        answer = runtime.answer("질문", card)
        assert answer.text == "결정론 응답"

    def test_ClaudeApiRuntime은_AgentRuntime_포트를_만족한다(self, card: AgentCard) -> None:
        from agent_org_network.runtime import AgentRuntime

        transport = StubProviderTransport(chunks=["답"])
        runtime: AgentRuntime = ClaudeApiRuntime(transport=transport)
        answer = runtime.answer("질문", card)
        assert isinstance(answer, Answer)


# ---------------------------------------------------------------------------
# answer_stream — ProviderApiRuntime 스트리밍 형제 (ADR 0031) [게이트 내·결정론]
# ---------------------------------------------------------------------------


class TestProviderAnswerStream:
    def test_ClaudeApiRuntime은_StreamingRuntime을_만족한다(self) -> None:
        transport = StubProviderTransport(chunks=["환", "불 ", "규정"])
        runtime = ClaudeApiRuntime(transport=transport)
        assert isinstance(runtime, StreamingRuntime)

    def test_CodexApiRuntime도_StreamingRuntime을_만족한다(self) -> None:
        transport = StubProviderTransport(chunks=["답"])
        runtime = CodexApiRuntime(transport=transport)
        assert isinstance(runtime, StreamingRuntime)

    def test_answer_stream은_청크마다_AnswerChunk를_흘린다(self, card: AgentCard) -> None:
        transport = StubProviderTransport(chunks=["환", "불 ", "규정"])
        runtime = ClaudeApiRuntime(transport=transport)
        chunks = list(runtime.answer_stream("환불 규정?", card))
        assert chunks == [
            AnswerChunk(text_delta="환"),
            AnswerChunk(text_delta="불 "),
            AnswerChunk(text_delta="규정"),
        ]

    def test_answer_stream은_여러_델타를_낸다_폴백_아님(self, card: AgentCard) -> None:
        transport = StubProviderTransport(chunks=["환", "불 ", "규정"])
        runtime = ClaudeApiRuntime(transport=transport)
        chunks = list(runtime.answer_stream("환불 규정?", card))
        assert len(chunks) == 3  # 블로킹 1델타 폴백이 아니라 실제 다중 델타

    def test_answer_stream은_빈_청크를_스킵한다(self, card: AgentCard) -> None:
        transport = StubProviderTransport(chunks=["환", "", "불"])
        runtime = ClaudeApiRuntime(transport=transport)
        chunks = list(runtime.answer_stream("질문", card))
        assert chunks == [AnswerChunk(text_delta="환"), AnswerChunk(text_delta="불")]

    def test_answer_stream_델타_조립은_answer_text와_같다(self, card: AgentCard) -> None:
        transport = StubProviderTransport(chunks=["환", "불 ", "규정"])
        runtime = ClaudeApiRuntime(transport=transport)
        streamed_text = "".join(c.text_delta for c in runtime.answer_stream("환불 규정?", card))
        blocking_text = runtime.answer("환불 규정?", card).text
        assert streamed_text == blocking_text == "환불 규정"

    def test_LocalStreamingDispatcher가_다중_델타를_흘린다(self, card: AgentCard) -> None:
        from agent_org_network.dispatch import LocalStreamingDispatcher

        transport = StubProviderTransport(chunks=["환", "불 ", "규정"])
        runtime = ClaudeApiRuntime(transport=transport)
        dispatcher = LocalStreamingDispatcher(runtime)
        streamed = dispatcher.dispatch_stream("환불 규정?", card)
        deltas = [chunk.text_delta for chunk in streamed]
        assert deltas == ["환", "불 ", "규정"]  # 다중 델타(폴백 1델타 아님)
        assert streamed.completed.text == "환불 규정"
        assert streamed.completed.sources == tuple(card.knowledge_sources)


# ---------------------------------------------------------------------------
# 슬라이스 (a) — 후속 공급자 자리 (NotImplementedError)
# ---------------------------------------------------------------------------


class TestFutureProviderStubs:
    def test_CodexApiRuntime은_AgentRuntime_포트를_구현한다(self, card: AgentCard) -> None:
        """CodexApiRuntime은 슬라이스 1에서 StubTransport로 구현 완료."""
        from agent_org_network.runtime import AgentRuntime

        transport = StubProviderTransport(chunks=["codex 답"])
        runtime: AgentRuntime = CodexApiRuntime(transport=transport)
        answer = runtime.answer("질문", card)
        assert isinstance(answer, Answer)

    def test_GeminiApiRuntime_answer는_NotImplementedError(self, card: AgentCard) -> None:
        runtime = GeminiApiRuntime()
        with pytest.raises(NotImplementedError):
            runtime.answer("질문", card)


# ---------------------------------------------------------------------------
# 슬라이스 (b) — build_provider_request (순수 함수)
# ---------------------------------------------------------------------------


class TestBuildProviderRequest:
    def test_ProviderRequest를_반환한다(self, card: AgentCard) -> None:
        req = build_provider_request("질문입니다", card)
        assert isinstance(req, ProviderRequest)

    def test_model_필드가_있다(self, card: AgentCard) -> None:
        req = build_provider_request("질문", card)
        assert req.model != ""

    def test_messages_필드가_있다(self, card: AgentCard) -> None:
        req = build_provider_request("질문", card)
        assert len(req.messages) >= 1

    def test_question이_messages에_포함된다(self, card: AgentCard) -> None:
        question = "환불 정책이 어떻게 되나요?"
        req = build_provider_request(question, card)
        all_content = " ".join(str(m) for m in req.messages)
        assert question in all_content

    def test_context_기본값은_None(self, card: AgentCard) -> None:
        req = build_provider_request("질문", card)
        assert isinstance(req, ProviderRequest)

    def test_context_주입_가능(self, card: AgentCard) -> None:
        req = build_provider_request("질문", card, context="이전 맥락")
        assert isinstance(req, ProviderRequest)

    def test_agent_id가_비용_태깅으로_실린다(self, card: AgentCard) -> None:
        # Phase 12·ADR 0033 결정 2 — 중앙 조직 키 1개 하에서 담당자별 비용 태깅(식별자만).
        req = build_provider_request("질문", card)
        assert req.agent_id == "cs_ops"


# ---------------------------------------------------------------------------
# Phase 12 (C)·ADR 0033 결정 2 — 중앙 조직 API 키 로딩(env만·로그 미노출)
# ---------------------------------------------------------------------------


class TestCentralOrgKeyLoading:
    def test_AON_PROVIDER_KEY가_우선(self, monkeypatch: "pytest.MonkeyPatch") -> None:
        from agent_org_network.provider_transport_anthropic import load_central_org_key

        monkeypatch.setenv("AON_PROVIDER_KEY", "sk-central-org")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-owner")
        assert load_central_org_key() == "sk-central-org"

    def test_ANTHROPIC_API_KEY로_폴백(self, monkeypatch: "pytest.MonkeyPatch") -> None:
        from agent_org_network.provider_transport_anthropic import load_central_org_key

        monkeypatch.delenv("AON_PROVIDER_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-owner")
        assert load_central_org_key() == "sk-owner"

    def test_둘다_미설정이면_None(self, monkeypatch: "pytest.MonkeyPatch") -> None:
        from agent_org_network.provider_transport_anthropic import load_central_org_key

        monkeypatch.delenv("AON_PROVIDER_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert load_central_org_key() is None

    def test_순수_함수_동일_입력_동일_출력(self, card: AgentCard) -> None:
        req1 = build_provider_request("질문", card)
        req2 = build_provider_request("질문", card)
        assert req1 == req2

    def test_SDK_IO_0_외부_호출_없음(self, card: AgentCard) -> None:
        req = build_provider_request("질문", card, context=None)
        assert isinstance(req, ProviderRequest)

    def test_system에_team이_실린다(self, card: AgentCard) -> None:
        req = build_provider_request("질문", card)
        assert card.team in req.system

    def test_system에_owner가_실린다(self, card: AgentCard) -> None:
        req = build_provider_request("질문", card)
        assert card.owner in req.system

    def test_system에_summary가_실린다(self, card: AgentCard) -> None:
        req = build_provider_request("질문", card)
        assert card.summary in req.system

    def test_system에_domains가_실린다(self, card: AgentCard) -> None:
        req = build_provider_request("질문", card)
        for domain in card.domains:
            assert domain in req.system

    def test_system에_knowledge_sources가_실린다(self, card: AgentCard) -> None:
        req = build_provider_request("질문", card)
        for src in card.knowledge_sources:
            assert src in req.system

    def test_system에_can_answer가_있으면_실린다(self) -> None:
        card = AgentCard(
            agent_id="hr-ops",
            owner="carol",
            team="HR팀",
            summary="HR 운영 담당",
            domains=["채용"],
            last_reviewed_at=date(2026, 6, 27),
            can_answer=["연차 정책", "복리후생"],
        )
        req = build_provider_request("질문", card)
        assert "연차 정책" in req.system
        assert "복리후생" in req.system

    def test_system에_can_answer_없으면_해당_줄_없다(self, minimal_card: AgentCard) -> None:
        req = build_provider_request("질문", minimal_card)
        assert "답할 수 있는 것" not in req.system

    def test_system에_domains_없으면_해당_줄_없다(self) -> None:
        card = AgentCard(
            agent_id="solo-agent",
            owner="dave",
            team="팀없음",
            summary="단독 담당",
            domains=[],
            last_reviewed_at=date(2026, 6, 27),
        )
        req = build_provider_request("질문", card)
        assert "담당 도메인" not in req.system

    def test_system에_knowledge_sources_없으면_해당_줄_없다(self, minimal_card: AgentCard) -> None:
        req = build_provider_request("질문", minimal_card)
        assert "근거 출처" not in req.system

    def test_system은_비어있지_않다(self, card: AgentCard) -> None:
        req = build_provider_request("질문", card)
        assert req.system != ""


# ---------------------------------------------------------------------------
# 슬라이스 (b) — assemble_stream (순수 함수)
# ---------------------------------------------------------------------------


class TestAssembleStream:
    def test_청크_순서대로_조립(self) -> None:
        chunks = ["안녕", "하세요", " 반갑습니다"]
        result = assemble_stream(chunks)
        assert result == "안녕하세요 반갑습니다"

    def test_단일_청크(self) -> None:
        assert assemble_stream(["전체 텍스트"]) == "전체 텍스트"

    def test_빈_시퀀스(self) -> None:
        assert assemble_stream([]) == ""

    def test_제너레이터_입력(self) -> None:
        def gen() -> Iterable[str]:
            yield "A"
            yield "B"
            yield "C"

        assert assemble_stream(gen()) == "ABC"

    def test_빈_문자열_청크_포함(self) -> None:
        chunks = ["A", "", "B"]
        assert assemble_stream(chunks) == "AB"

    def test_순수_함수_동일_입력_동일_출력(self) -> None:
        chunks = ["x", "y", "z"]
        assert assemble_stream(chunks) == assemble_stream(chunks)

    def test_공백_보존(self) -> None:
        chunks = ["첫 ", "번째 ", "문장."]
        assert assemble_stream(chunks) == "첫 번째 문장."


# ---------------------------------------------------------------------------
# 슬라이스 (b) — map_response_to_answer (순수 함수, 노출 불변식)
# ---------------------------------------------------------------------------


class TestMapResponseToAnswer:
    def test_Answer를_반환한다(self, card: AgentCard) -> None:
        answer = map_response_to_answer("텍스트 응답", card)
        assert isinstance(answer, Answer)

    def test_text_필드가_매핑된다(self, card: AgentCard) -> None:
        answer = map_response_to_answer("정확한 답변", card)
        assert answer.text == "정확한 답변"

    def test_sources는_카드_knowledge_sources_투영(self, card: AgentCard) -> None:
        answer = map_response_to_answer("답", card)
        assert answer.sources == tuple(card.knowledge_sources)

    def test_mode는_full이다(self, card: AgentCard) -> None:
        answer = map_response_to_answer("답", card)
        assert answer.mode == "full"

    def test_snapshot_sha는_None이다(self, card: AgentCard) -> None:
        answer = map_response_to_answer("답", card)
        assert answer.snapshot_sha is None

    def test_Answer_계약_보존_새_필드_없음(self, card: AgentCard) -> None:
        answer = map_response_to_answer("답", card)
        assert set(answer.__dataclass_fields__) == {"text", "sources", "mode", "snapshot_sha"}

    def test_knowledge_sources_없는_카드는_빈_sources(self, minimal_card: AgentCard) -> None:
        answer = map_response_to_answer("답", minimal_card)
        assert answer.sources == ()

    def test_고정_응답_fixture_결정론(self, card: AgentCard) -> None:
        fixed_text = "고정된 응답 텍스트입니다."
        answer1 = map_response_to_answer(fixed_text, card)
        answer2 = map_response_to_answer(fixed_text, card)
        assert answer1 == answer2

    def test_내부값_비밀_누출_없음(self, card: AgentCard) -> None:
        secret = "secret_token_abc123"
        answer = map_response_to_answer("정상 답변", card)
        assert secret not in answer.text
        assert not any(secret in s for s in answer.sources)

    def test_순수_함수_SDK_IO_0(self, card: AgentCard) -> None:
        answer = map_response_to_answer("답변", card)
        assert isinstance(answer, Answer)

    def test_빈_knowledge_sources_tuple_반환(self) -> None:
        card = AgentCard(
            agent_id="test-agent",
            owner="user",
            team="팀",
            summary="요약",
            domains=["도메인"],
            last_reviewed_at=date(2026, 6, 27),
            knowledge_sources=[],
        )
        answer = map_response_to_answer("답", card)
        assert answer.sources == ()
        assert isinstance(answer.sources, tuple)


# ---------------------------------------------------------------------------
# Answer 계약 보존 — 통합 불변식 테스트
# ---------------------------------------------------------------------------


class TestAnswerContractInvariant:
    def test_ClaudeApiRuntime_answer는_Answer_필드_완전성_보장(self, card: AgentCard) -> None:
        transport = StubProviderTransport(chunks=["답"])
        runtime = ClaudeApiRuntime(transport=transport)
        answer = runtime.answer("질문", card)
        assert hasattr(answer, "text")
        assert hasattr(answer, "sources")
        assert hasattr(answer, "mode")
        assert hasattr(answer, "snapshot_sha")

    def test_map_response_to_answer는_Answer_frozen이다(self, card: AgentCard) -> None:
        answer = map_response_to_answer("답", card)
        with pytest.raises((AttributeError, TypeError)):
            answer.text = "변경 시도"  # type: ignore[misc]

    def test_answer_sources는_항상_tuple이다(self, card: AgentCard) -> None:
        transport = StubProviderTransport(chunks=["답"])
        runtime = ClaudeApiRuntime(transport=transport)
        answer = runtime.answer("질문", card)
        assert isinstance(answer.sources, tuple)

    def test_answer_mode는_AnswerMode_리터럴이다(self, card: AgentCard) -> None:
        transport = StubProviderTransport(chunks=["답"])
        runtime = ClaudeApiRuntime(transport=transport)
        answer = runtime.answer("질문", card)
        assert answer.mode in ("draft_only", "full", "backup")


# ---------------------------------------------------------------------------
# 일시 장애 재시도 — ProviderApiRuntime.answer/answer_stream (provider_retry 통합)
# ---------------------------------------------------------------------------


class _FakeStatusError(Exception):
    """anthropic APIStatusError의 status_code 규약 흉내(SDK import 0)."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class _RecordingSleeper:
    """호출된 지연을 기록하는 no-op sleeper — 실 sleep 0."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class _FlakyTransport:
    """지정 횟수만큼 예외를 던진 뒤 청크를 내는 fake transport(재시도 복구 검증)."""

    def __init__(self, *, fail_times: int, exc: Exception, chunks: list[str]) -> None:
        self._fail_times = fail_times
        self._exc = exc
        self._chunks = chunks
        self.calls = 0

    def __call__(self, request: ProviderRequest) -> Iterable[str]:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._exc
        return iter(self._chunks)


class _MidStreamFailTransport:
    """첫 청크는 내고 그 다음에 예외를 던지는 fake transport(스트림 중간 실패)."""

    def __init__(self, *, first: str, exc: Exception) -> None:
        self._first = first
        self._exc = exc
        self.calls = 0

    def __call__(self, request: ProviderRequest) -> Iterable[str]:
        self.calls += 1
        yield self._first
        raise self._exc


class TestProviderRuntimeRetry:
    def test_answer는_일시_실패_후_재시도로_복구한다(self, card: AgentCard) -> None:
        transport = _FlakyTransport(
            fail_times=2, exc=_FakeStatusError(503), chunks=["복구된 ", "답변"]
        )
        sleeper = _RecordingSleeper()
        runtime = ClaudeApiRuntime(
            transport=transport,
            retry_policy=RetryPolicy(max_attempts=3, backoff_base_s=0.5),
            sleeper=sleeper,
        )
        answer = runtime.answer("질문", card)
        assert answer.text == "복구된 답변"
        assert transport.calls == 3  # 2회 실패 + 1회 성공
        assert sleeper.calls == [0.5, 1.0]  # 지수 백오프·실 sleep 0

    def test_answer_401은_ProviderAuthError로_승격한다(self, card: AgentCard) -> None:
        transport = _FlakyTransport(fail_times=5, exc=_FakeStatusError(401), chunks=["never"])
        sleeper = _RecordingSleeper()
        runtime = ClaudeApiRuntime(transport=transport, sleeper=sleeper)
        with pytest.raises(ProviderAuthError):
            runtime.answer("질문", card)
        assert transport.calls == 1  # 재시도 없음
        assert sleeper.calls == []

    def test_answer_재시도_소진은_원_예외_그대로_재던진다(self, card: AgentCard) -> None:
        transport = _FlakyTransport(fail_times=99, exc=_FakeStatusError(503), chunks=[])
        runtime = ClaudeApiRuntime(
            transport=transport,
            retry_policy=RetryPolicy(max_attempts=2, backoff_base_s=0.1),
            sleeper=_RecordingSleeper(),
        )
        with pytest.raises(_FakeStatusError) as excinfo:
            runtime.answer("질문", card)
        assert excinfo.value.status_code == 503  # ProviderAuthError로 뭉개지 않음

    def test_answer_400은_재시도_없이_재던진다(self, card: AgentCard) -> None:
        transport = _FlakyTransport(fail_times=99, exc=_FakeStatusError(400), chunks=[])
        sleeper = _RecordingSleeper()
        runtime = ClaudeApiRuntime(transport=transport, sleeper=sleeper)
        with pytest.raises(_FakeStatusError):
            runtime.answer("질문", card)
        assert transport.calls == 1
        assert sleeper.calls == []

    def test_answer_stub_transport는_무회귀_재시도_경로_무영향(self, card: AgentCard) -> None:
        transport = StubProviderTransport(chunks=["정상 답변"])
        sleeper = _RecordingSleeper()
        runtime = ClaudeApiRuntime(transport=transport, sleeper=sleeper)
        answer = runtime.answer("질문", card)
        assert answer.text == "정상 답변"
        assert sleeper.calls == []  # 첫 시도 성공 — 재시도 없음

    def test_answer_stream_시작_전_실패는_재시도한다(self, card: AgentCard) -> None:
        transport = _FlakyTransport(
            fail_times=1, exc=_FakeStatusError(429), chunks=["환", "불"]
        )
        sleeper = _RecordingSleeper()
        runtime = ClaudeApiRuntime(
            transport=transport,
            retry_policy=RetryPolicy(max_attempts=3, backoff_base_s=0.5),
            sleeper=sleeper,
        )
        chunks = list(runtime.answer_stream("질문", card))
        assert chunks == [AnswerChunk(text_delta="환"), AnswerChunk(text_delta="불")]
        assert transport.calls == 2  # 1회 실패 + 1회 성공
        assert sleeper.calls == [0.5]

    def test_answer_stream_중간_실패는_재시도하지_않고_전파한다(self, card: AgentCard) -> None:
        # 첫 델타가 이미 흘렀으므로 중간 실패는 재시도 금지 — 소비 중 예외로 전파.
        transport = _MidStreamFailTransport(first="부분", exc=_FakeStatusError(503))
        sleeper = _RecordingSleeper()
        runtime = ClaudeApiRuntime(
            transport=transport,
            retry_policy=RetryPolicy(max_attempts=3, backoff_base_s=0.5),
            sleeper=sleeper,
        )
        stream = runtime.answer_stream("질문", card)
        collected: list[AnswerChunk] = []
        with pytest.raises(_FakeStatusError):
            for chunk in stream:
                collected.append(chunk)
        assert collected == [AnswerChunk(text_delta="부분")]  # 첫 델타는 흘렀다
        assert transport.calls == 1  # 재시도 없음(중간 실패)
        assert sleeper.calls == []

    def test_answer_stream_401은_ProviderAuthError로_승격한다(self, card: AgentCard) -> None:
        transport = _FlakyTransport(fail_times=5, exc=_FakeStatusError(403), chunks=["never"])
        runtime = ClaudeApiRuntime(transport=transport, sleeper=_RecordingSleeper())
        with pytest.raises(ProviderAuthError):
            list(runtime.answer_stream("질문", card))
        assert transport.calls == 1  # 재시도 없음
