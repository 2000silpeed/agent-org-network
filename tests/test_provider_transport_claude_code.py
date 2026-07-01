"""ClaudeCodeTransport 결정론 테스트 — runner seam 주입(실 subprocess 0).

`ClaudeCodeTransport`는 `ProviderTransport`(provider_runtime.py) Protocol을 만족하는
*실* transport로, 기본 runner는 `claude -p`(구독 답의 공식 경로·게이트 밖) subprocess다.
여기서는 fake runner를 주입해 (a) system+user 프롬프트 평탄화, (b) model 전달,
(c) 청크 yield, (d) `LlmAuthor`에 ProviderTransport로 꽂혀 parse_split까지 관통을
결정론으로 검증한다 — 실 subprocess·네트워크 0(provider_transport_anthropic 패턴).
"""

from __future__ import annotations

from agent_org_network.okf_authoring import LlmAuthor, derive_concept_key
from agent_org_network.provider_runtime import ProviderRequest, ProviderTransport
from agent_org_network.provider_transport_claude_code import (
    ClaudeCodeCall,
    ClaudeCodeTransport,
)


class _FakeRunner:
    """주입 runner 더블 — 호출 인자를 기록하고 고정 텍스트를 반환한다."""

    def __init__(self, reply: str = "ok") -> None:
        self._reply = reply
        self.seen: ClaudeCodeCall | None = None

    def __call__(self, call: ClaudeCodeCall) -> str:
        self.seen = call
        return self._reply


def test_프롬프트_평탄화_system과_user가_포함된다() -> None:
    runner = _FakeRunner()
    transport = ClaudeCodeTransport(runner=runner)
    request = ProviderRequest(
        model="claude-sonnet-4-6",
        system="시스템 지시문입니다.",
        messages=[
            {"role": "user", "content": "첫째 문단"},
            {"role": "user", "content": "둘째 문단"},
        ],
    )
    list(transport(request))  # 소비
    assert runner.seen is not None
    prompt = runner.seen.prompt
    assert "시스템 지시문입니다." in prompt
    assert "첫째 문단" in prompt
    assert "둘째 문단" in prompt
    # system이 user보다 앞에 평탄화된다(맥락 우선).
    assert prompt.index("시스템 지시문입니다.") < prompt.index("첫째 문단")
    assert prompt.index("첫째 문단") < prompt.index("둘째 문단")


def test_model이_runner로_전달된다() -> None:
    runner = _FakeRunner()
    transport = ClaudeCodeTransport(runner=runner)
    request = ProviderRequest(model="claude-sonnet-4-6", system="s", messages=[])
    list(transport(request))
    assert runner.seen is not None
    assert runner.seen.model == "claude-sonnet-4-6"


def test_request_model_비면_생성자_기본_model_사용() -> None:
    runner = _FakeRunner()
    transport = ClaudeCodeTransport(runner=runner, model="claude-haiku-4-6")
    request = ProviderRequest(model="", system="s", messages=[])
    list(transport(request))
    assert runner.seen is not None
    assert runner.seen.model == "claude-haiku-4-6"


def test_청크_yield_단일_청크로_응답_전체() -> None:
    runner = _FakeRunner(reply="응답 텍스트 전체")
    transport = ClaudeCodeTransport(runner=runner)
    request = ProviderRequest(model="m", system="s", messages=[])
    chunks = list(transport(request))
    assert chunks == ["응답 텍스트 전체"]


def test_ProviderTransport_Protocol_만족() -> None:
    transport: ProviderTransport = ClaudeCodeTransport(runner=_FakeRunner())
    assert callable(transport)


def test_LlmAuthor에_꽂혀_split_관통() -> None:
    """fake runner가 정상 JSON 배열을 반환 → LlmAuthor.split이 OkfDocumentDraft를 낸다."""
    split_json = (
        '[{"concept_id": "refund-policy", "title": "환불 정책",'
        ' "body": "환불은 7일 이내", "core_question": "환불은 어떻게?",'
        ' "domain": "환불", "type": null}]'
    )
    runner = _FakeRunner(reply=split_json)
    transport = ClaudeCodeTransport(runner=runner, model="claude-sonnet-4-6")
    author = LlmAuthor(transport, model="claude-sonnet-4-6")

    from agent_org_network.okf_authoring import RawSource

    drafts = author.split([RawSource(source_id="src1", content="환불 정책 본문")])
    assert len(drafts) == 1
    # concept_id는 LLM이 낸 값이 아니라 derive_concept_key(domain, title)로 도출(ADR 0032 B2)
    assert drafts[0].concept_id == derive_concept_key("환불", "환불 정책")
    assert drafts[0].domain == "환불"
    # split 프롬프트가 runner까지 닿았다(source 내용 포함).
    assert runner.seen is not None
    assert "환불 정책 본문" in runner.seen.prompt
